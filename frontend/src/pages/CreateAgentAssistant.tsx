import type { CSSProperties } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import {
  Btn,
  Chip,
  ConfirmDialog,
  LoadError,
  Markdown,
  Panel,
  useToast,
  ViewHead,
} from "../components";
import type { ChipTone } from "../components";
import type {
  AgentInfo,
  AssistantApproval,
  AssistantCatalog,
  AssistantConversationDetail,
  AssistantConversationSummary,
  AssistantMemoryMode,
  AssistantMessage,
  AssistantProposal,
  AssistantProposalContent,
  AssistantProposalStatus,
  AssistantStatus,
  JobInfo,
  StageInfo,
} from "../lib/api";
import {
  api,
  ApiError,
  AUTH_UNAUTHORIZED_EVENT,
  errorMessage,
} from "../lib/api";
import { MODEL_CATALOG, type ModelSource } from "../lib/models";
import { useWorkspace } from "../workspace/workspace-context";

/**
 * Architect assistant (SE-039): a member conversation with the protected
 * `aws-agent-solution-architect` preset that ends in an inert, reviewable proposal
 * for ONE new managed Harness. Discussion never writes AWS; the proposal is shown
 * verbatim with the exact resources it binds to; only APPROVE & DEPLOY (a separate
 * authenticated call naming the exact revision + content hash) creates the agent
 * through the normal deploy job.
 *
 * Staleness model: every asynchronous outcome carries the *operation generation* it
 * started under (a counter bumped on every conversation selection, workspace change
 * and unmount) and is dropped when the generation moved on — so a slower load of
 * conversation A can never overwrite the newer selection B, a pending turn or
 * approval reload cannot pull an old conversation back, and a job poll that
 * resolves after the user moved on schedules nothing. Load callbacks read `t` and
 * the toast through refs so a locale change re-renders without re-running mount
 * effects (which would drop an unsaved draft or a streaming reply).
 */

const STATUS_TONE: Record<AssistantProposalStatus, ChipTone> = {
  draft: "amber",
  invalid: "crit",
  approved: "good",
  rejected: "muted",
  superseded: "muted",
};
const JOB_POLL_MS = 3000;
const NAME_RE = /^[a-z][a-z0-9-]{2,47}$/;
const MODEL_RE = /^[A-Za-z0-9][A-Za-z0-9._:/-]{2,120}$/;
const PROPOSAL_FENCE_RE =
  /```launchpad-proposal[ \t]*\r?\n[\s\S]*?\r?\n[ \t]*```/g;

interface LiveMessage {
  role: "user" | "assistant" | "tool" | "error";
  text: string;
  name?: string | null;
  streaming?: boolean;
}

/** What the approve dialog was opened on — submitted verbatim, never "the latest". */
interface PinnedApproval {
  conversationId: string;
  revision: number;
  hash: string;
  name: string;
}

async function* sseEvents(
  res: Response,
): AsyncGenerator<{ event: string; data: never }> {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) yield { event, data: JSON.parse(data) as never };
    }
  }
}

function toLive(rows: AssistantMessage[]): LiveMessage[] {
  return rows.map((m) => ({ role: m.role, text: m.text, name: m.name }));
}

/** The typed proposal pane shows the block; the transcript shows a pointer instead. */
function stripProposalBlock(text: string, marker: string): string {
  return text.replace(PROPOSAL_FENCE_RE, `> ${marker}`);
}

type EditDraft = Pick<
  AssistantProposalContent,
  | "name"
  | "model_id"
  | "model_source"
  | "system_prompt"
  | "tools"
  | "skills"
  | "knowledge_bases"
  | "memory"
  | "max_iterations"
  | "timeout_seconds"
>;

function draftFrom(content: AssistantProposal["content"]): EditDraft {
  return {
    name: String(content.name ?? ""),
    model_id: String(content.model_id ?? MODEL_CATALOG.bedrock[0].model_id),
    model_source: (content.model_source as ModelSource) ?? "bedrock",
    system_prompt: String(content.system_prompt ?? ""),
    tools: Array.isArray(content.tools) ? content.tools.map(String) : [],
    skills: Array.isArray(content.skills) ? content.skills.map(String) : [],
    knowledge_bases: Array.isArray(content.knowledge_bases)
      ? content.knowledge_bases.map(String)
      : [],
    memory: content.memory === "workspace" ? "workspace" : "disabled",
    max_iterations: Number(content.max_iterations ?? 10),
    timeout_seconds: Number(content.timeout_seconds ?? 300),
  };
}

const isUnauthorized = (err: unknown) =>
  err instanceof ApiError &&
  (err.code === "http.401" ||
    err.code === "http.403" ||
    err.code === "auth.required" ||
    err.code === "workspace.forbidden" ||
    err.code === "auth.permission_required");

export function CreateAgentAssistant() {
  const { t, i18n } = useTranslation();
  const toast = useToast();
  const navigate = useNavigate();
  const { can, isAdmin } = useAuth();
  const { current } = useWorkspace();
  const workspaceId = current?.id ?? null;
  const [searchParams, setSearchParams] = useSearchParams();
  const linkedConversation = searchParams.get("conversation");

  const [status, setStatus] = useState<AssistantStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [conversations, setConversations] = useState<
    AssistantConversationSummary[]
  >([]);
  const [conversation, setConversation] =
    useState<AssistantConversationDetail | null>(null);
  const [messages, setMessages] = useState<LiveMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [approving, setApproving] = useState(false);
  const [editing, setEditing] = useState<EditDraft | null>(null);
  const [confirm, setConfirm] = useState<
    | { kind: "approve"; pin: PinnedApproval }
    | { kind: "reject"; revision: number }
    | null
  >(null);
  // outcome state is keyed by the approval it was polled for
  const [polled, setPolled] = useState<{
    jobId: string;
    job: JobInfo | null;
    agent: AgentInfo | null;
  }>({ jobId: "", job: null, agent: null });
  const threadRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // ---- staleness: workspace scope + operation generation ------------------------
  const alive = useRef(true);
  const scope = useRef(workspaceId);
  const generation = useRef(0);
  const bump = useCallback(() => {
    generation.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    return generation.current;
  }, []);
  useEffect(() => {
    alive.current = true;
    scope.current = workspaceId;
    return () => {
      alive.current = false;
      bump();
    };
  }, [workspaceId, bump]);
  const stillCurrent = useCallback(
    (startedIn: string | null, gen: number) =>
      alive.current &&
      scope.current === startedIn &&
      generation.current === gen,
    [],
  );
  // refs keep the load callbacks identity-stable across locale changes
  const tRef = useRef(t);
  tRef.current = t;
  const toastRef = useRef(toast);
  toastRef.current = toast;
  const apiMessage = useCallback(
    (err: unknown) =>
      err instanceof ApiError
        ? tRef.current(`apiErrors.${err.code}`, err.message)
        : errorMessage(err),
    [],
  );

  const loadStatus = useCallback(() => {
    const startedIn = scope.current;
    const gen = generation.current;
    setStatusError(null);
    void api
      .assistantStatus()
      .then((res) => {
        if (stillCurrent(startedIn, gen)) setStatus(res);
      })
      .catch((err: unknown) => {
        if (stillCurrent(startedIn, gen)) setStatusError(apiMessage(err));
      });
  }, [apiMessage, stillCurrent]);

  const loadConversations = useCallback(() => {
    const startedIn = scope.current;
    const gen = generation.current;
    void api
      .assistantConversations()
      .then((res) => {
        if (stillCurrent(startedIn, gen)) setConversations(res.conversations);
      })
      .catch(() => {
        /* the list is secondary; the status panel reports the real failure */
      });
  }, [stillCurrent]);

  const setLinked = useCallback(
    (id: string | null) =>
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (id) next.set("conversation", id);
          else next.delete("conversation");
          return next;
        },
        { replace: true },
      ),
    [setSearchParams],
  );

  /** Select a conversation: a new operation generation, so every earlier in-flight
   * load / stream / poll for another conversation is dropped when it resolves. */
  const selectConversation = useCallback(
    (id: string) => {
      const startedIn = scope.current;
      const gen = bump();
      setEditing(null);
      setConfirm(null);
      setLinked(id);
      void api
        .assistantConversation(id)
        .then((detail) => {
          if (!stillCurrent(startedIn, gen)) return;
          setConversation(detail);
          setMessages(toLive(detail.messages));
        })
        .catch((err: unknown) => {
          if (!stillCurrent(startedIn, gen)) return;
          toastRef.current(apiMessage(err));
          if (
            err instanceof ApiError &&
            err.code === "assistant.conversation_not_found"
          ) {
            setConversation(null);
            setMessages([]);
            setLinked(null);
          }
        });
    },
    [apiMessage, bump, setLinked, stillCurrent],
  );

  // Workspace (re)mount: reset and re-read. Depends on the workspace only — a
  // locale change must not run this (it would drop drafts and streams).
  useEffect(() => {
    if (workspaceId === null) return;
    setConversation(null);
    setMessages([]);
    setEditing(null);
    setConfirm(null);
    setPolled({ jobId: "", job: null, agent: null });
    loadStatus();
    loadConversations();
  }, [loadConversations, loadStatus, workspaceId]);

  // Deep link: opened once per (workspace, linked id) when the assistant is
  // available and nothing else is selected yet.
  const bootedLink = useRef<string | null>(null);
  useEffect(() => {
    if (workspaceId === null || !status?.available || !linkedConversation)
      return;
    const key = `${workspaceId}:${linkedConversation}`;
    if (bootedLink.current === key || conversation?.id === linkedConversation)
      return;
    bootedLink.current = key;
    selectConversation(linkedConversation);
  }, [
    conversation?.id,
    linkedConversation,
    selectConversation,
    status?.available,
    workspaceId,
  ]);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [messages]);

  const proposals = useMemo(() => conversation?.proposals ?? [], [conversation]);
  const latest = useMemo(
    () => (proposals.length ? proposals[proposals.length - 1] : null),
    [proposals],
  );
  const approvedRevisions = useMemo(
    () => proposals.filter((p) => p.status === "approved" && p.approval),
    [proposals],
  );
  // The outcome shown belongs to the latest revision when that one was approved,
  // otherwise to the most recent approved revision; older approvals are listed.
  const shownApproved = useMemo(() => {
    if (latest?.status === "approved") return latest;
    return approvedRevisions.length
      ? approvedRevisions[approvedRevisions.length - 1]
      : null;
  }, [approvedRevisions, latest]);
  const approval: AssistantApproval | null = shownApproved?.approval ?? null;
  const olderApprovals = approvedRevisions.filter(
    (p) => p.id !== shownApproved?.id,
  );

  // A dialog opened on revision N is invalidated the moment the latest revision or
  // its hash changes (a turn completed, an edit landed): never approve a different
  // review silently. The backend refuses a stale pin anyway; this keeps the UI honest.
  useEffect(() => {
    if (!confirm || confirm.kind !== "approve") return;
    if (
      !latest ||
      conversation?.id !== confirm.pin.conversationId ||
      latest.revision !== confirm.pin.revision ||
      latest.content_hash !== confirm.pin.hash ||
      latest.status !== "draft"
    ) {
      setConfirm(null);
      toastRef.current(tRef.current("assistantPage.dialogStale"));
    }
  }, [confirm, conversation?.id, latest]);

  // Deployment outcome: poll the ordinary job + agent while the job is live. Keyed
  // by (generation, job id); a resolve after cleanup neither writes nor reschedules.
  useEffect(() => {
    const jobId = approval?.job_id;
    const agentId = approval?.agent_id;
    if (!jobId || !agentId) {
      setPolled({ jobId: "", job: null, agent: null });
      return;
    }
    const startedIn = scope.current;
    const gen = generation.current;
    let cancelled = false;
    let timer: number | undefined;
    const tick = () => {
      void Promise.all([api.getJob(jobId), api.getAgent(agentId)])
        .then(([j, a]) => {
          if (cancelled || !stillCurrent(startedIn, gen)) return;
          setPolled({ jobId, job: j, agent: a });
          if (j.status === "queued" || j.status === "running") {
            timer = window.setTimeout(tick, JOB_POLL_MS);
          }
        })
        .catch(() => {
          if (cancelled || !stillCurrent(startedIn, gen)) return;
          timer = window.setTimeout(tick, JOB_POLL_MS * 2);
        });
    };
    tick();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [approval?.agent_id, approval?.job_id, conversation?.id, stillCurrent]);

  const reload = async (
    conversationId: string,
    startedIn: string | null,
    gen: number,
  ) => {
    const detail = await api.assistantConversation(conversationId);
    if (!stillCurrent(startedIn, gen) || detail.id !== conversationId)
      return null;
    setConversation(detail);
    setMessages(toLive(detail.messages));
    loadConversations();
    return detail;
  };

  const newConversation = async () => {
    const startedIn = scope.current;
    const gen = bump();
    setBusy(true);
    try {
      const detail = await api.assistantCreateConversation();
      if (!stillCurrent(startedIn, gen)) return;
      setConversation(detail);
      setMessages([]);
      setEditing(null);
      setConfirm(null);
      setLinked(detail.id);
      loadConversations();
      if (detail.catalog.warnings.length) {
        toast(
          t("assistantPage.catalogWarnings", {
            list: detail.catalog.warnings.join("; "),
          }),
        );
      }
    } catch (err) {
      if (!stillCurrent(startedIn, gen)) return;
      toast(apiMessage(err));
      if (err instanceof ApiError && err.code === "assistant.unavailable")
        loadStatus();
    } finally {
      if (stillCurrent(startedIn, gen)) setBusy(false);
    }
  };

  const send = async () => {
    if (!conversation || !input.trim() || busy) return;
    const startedIn = scope.current;
    const gen = generation.current;
    const conversationId = conversation.id;
    const prompt = input;
    setInput("");
    setBusy(true);
    setMessages((m) => [
      ...m,
      { role: "user", text: prompt },
      { role: "assistant", text: "", streaming: true },
    ]);
    const controller = new AbortController();
    abortRef.current = controller;
    const append = (text: string) =>
      setMessages((m) => {
        const copy = [...m];
        const last = copy[copy.length - 1];
        if (last?.role === "assistant" && last.streaming) {
          copy[copy.length - 1] = { ...last, text: last.text + text };
        }
        return copy;
      });
    try {
      const res = await fetch(
        `/api/assistant/architect/conversations/${encodeURIComponent(conversationId)}/turns`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ prompt }),
          signal: controller.signal,
        },
      );
      if (!res.ok) {
        // same session semantics as the typed client: a 401 signs the console out
        if (res.status === 401)
          window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT));
        const body = (await res.json().catch(() => ({}))) as {
          code?: string;
          message?: string;
        };
        throw new ApiError(
          body.code ?? `http.${res.status}`,
          body.message ?? res.statusText,
          null,
        );
      }
      let proposal: AssistantProposal | null = null;
      for await (const evt of sseEvents(res)) {
        if (!stillCurrent(startedIn, gen)) return;
        const data = evt.data as Record<string, unknown>;
        if (evt.event === "delta") append(String(data.text ?? ""));
        else if (evt.event === "tool") {
          setMessages((m) => {
            const copy = [...m];
            const last = copy[copy.length - 1];
            if (last?.role === "assistant" && last.streaming) copy.pop();
            copy.push({
              role: "tool",
              text: "",
              name: String(data.name ?? ""),
            });
            copy.push(
              last?.role === "assistant" && last.streaming
                ? last
                : { role: "assistant", text: "", streaming: true },
            );
            return copy;
          });
        } else if (evt.event === "proposal")
          proposal = data as unknown as AssistantProposal;
        else if (evt.event === "error") {
          const code = typeof data.code === "string" ? data.code : null;
          setMessages((m) => [
            ...m.filter(
              (x) => !(x.role === "assistant" && x.streaming && !x.text),
            ),
            {
              role: "error",
              text: code
                ? tRef.current(`apiErrors.${code}`, String(data.message ?? ""))
                : String(data.message ?? ""),
            },
          ]);
        }
      }
      if (!stillCurrent(startedIn, gen)) return;
      setMessages((m) => m.map((x) => ({ ...x, streaming: false })));
      // re-read the server's transcript + proposals: the ledger is the truth
      await reload(conversationId, startedIn, gen);
      if (proposal && stillCurrent(startedIn, gen)) setEditing(null);
    } catch (err) {
      if (!stillCurrent(startedIn, gen) || controller.signal.aborted) return;
      setMessages((m) => [
        ...m.filter((x) => !(x.role === "assistant" && x.streaming && !x.text)),
        {
          role: "error",
          text: t("assistantPage.turnFailed", { msg: apiMessage(err) }),
        },
      ]);
      if (isUnauthorized(err)) toast(t("assistantPage.expiredSession"));
      else if (err instanceof ApiError && err.code === "assistant.unavailable")
        loadStatus();
    } finally {
      if (stillCurrent(startedIn, gen)) setBusy(false);
      if (abortRef.current === controller) abortRef.current = null;
    }
  };

  const refreshCatalog = async () => {
    if (!conversation) return;
    const startedIn = scope.current;
    const gen = generation.current;
    const conversationId = conversation.id;
    try {
      const res = await api.assistantRefreshCatalog(conversationId);
      if (!stillCurrent(startedIn, gen)) return;
      setConversation((c) =>
        c && c.id === conversationId ? { ...c, catalog: res.catalog } : c,
      );
      toast(t("assistantPage.catalogChanged"), "good");
    } catch (err) {
      if (stillCurrent(startedIn, gen)) toast(apiMessage(err));
    }
  };

  const approve = async (pin: PinnedApproval) => {
    const startedIn = scope.current;
    const gen = generation.current;
    setConfirm(null);
    if (conversation?.id !== pin.conversationId) return;
    setApproving(true);
    try {
      const res = await api.assistantApproveProposal(
        pin.conversationId,
        pin.revision,
        pin.hash,
      );
      if (!stillCurrent(startedIn, gen)) return;
      toast(
        res.started
          ? t("assistantPage.approvedToast", {
              job: (res.job_id ?? "").slice(0, 8),
            })
          : t("assistantPage.alreadyApprovedToast"),
        "good",
      );
      await reload(pin.conversationId, startedIn, gen);
    } catch (err) {
      if (!stillCurrent(startedIn, gen)) return;
      toast(apiMessage(err));
      if (isUnauthorized(err)) loadStatus();
      else await reload(pin.conversationId, startedIn, gen).catch(() => null);
    } finally {
      if (stillCurrent(startedIn, gen)) setApproving(false);
    }
  };

  const reject = async (revision: number) => {
    if (!conversation) return;
    const startedIn = scope.current;
    const gen = generation.current;
    const conversationId = conversation.id;
    setConfirm(null);
    try {
      await api.assistantRejectProposal(conversationId, revision);
      if (!stillCurrent(startedIn, gen)) return;
      toast(t("assistantPage.rejectedToast"), "good");
      setEditing(null);
      await reload(conversationId, startedIn, gen);
    } catch (err) {
      if (!stillCurrent(startedIn, gen)) return;
      toast(apiMessage(err));
      await reload(conversationId, startedIn, gen).catch(() => null);
    }
  };

  const editErrors = (draft: EditDraft): Record<string, boolean> => ({
    name: !NAME_RE.test(draft.name),
    model_id: !MODEL_RE.test(draft.model_id),
    system_prompt:
      draft.system_prompt.trim().length === 0 ||
      draft.system_prompt.length > 20000,
    max_iterations: !(draft.max_iterations >= 1 && draft.max_iterations <= 100),
    timeout_seconds: !(
      draft.timeout_seconds >= 10 && draft.timeout_seconds <= 3600
    ),
  });

  const saveEdit = async () => {
    if (!conversation || !latest || !editing) return;
    const errors = editErrors(editing);
    if (Object.values(errors).some(Boolean)) {
      toast(t("assistantPage.editInvalid"));
      return;
    }
    const startedIn = scope.current;
    const gen = generation.current;
    const conversationId = conversation.id;
    const base = latest.content;
    const content: AssistantProposalContent = {
      version: 1,
      ...editing,
      summary: typeof base.summary === "string" ? base.summary : "",
      requirements_baseline: Array.isArray(base.requirements_baseline)
        ? base.requirements_baseline
        : [],
      assumptions: Array.isArray(base.assumptions) ? base.assumptions : [],
      manual_tasks: Array.isArray(base.manual_tasks) ? base.manual_tasks : [],
      golden_tests: Array.isArray(base.golden_tests) ? base.golden_tests : [],
      evaluator_recommendations: Array.isArray(base.evaluator_recommendations)
        ? base.evaluator_recommendations
        : [],
    };
    try {
      const res = await api.assistantEditProposal(conversationId, content);
      if (!stillCurrent(startedIn, gen)) return;
      toast(
        t("assistantPage.editSavedToast", { n: res.proposal.revision }),
        "good",
      );
      setEditing(null);
      await reload(conversationId, startedIn, gen);
    } catch (err) {
      if (stillCurrent(startedIn, gen)) toast(apiMessage(err));
    }
  };

  const canDeploy =
    (status?.can_deploy ?? can("agents.deploy")) &&
    (status?.deploy_requirements.length ?? 0) === 0;
  const deployReason = !status?.can_deploy
    ? t("assistantPage.noDeployPermission")
    : status.deploy_requirements.length
      ? t("assistantPage.deployBlocked", {
          list: status.deploy_requirements.map((r) => r.message).join("; "),
        })
      : undefined;
  const dateLabel = (iso: string | null) =>
    iso ? new Date(iso).toLocaleString(i18n.language) : "";
  const meta = status
    ? t("assistantPage.meta", {
        label: status.preset.label,
        workspace: status.workspace_id,
        region: status.region,
      })
    : undefined;
  const proposalMarker = t("assistantPage.proposalInText");

  return (
    <section data-testid="assistant-page">
      <ViewHead
        kicker={t("assistantPage.kicker")}
        title={t("assistantPage.title")}
        meta={meta}
      />
      <div style={{ marginBottom: 12 }}>
        <Link className="assist-link" to="/create" data-testid="assistant-back">
          {t("assistantPage.backToCreate")}
        </Link>
      </div>

      {statusError && (
        <LoadError
          message={statusError}
          onRetry={loadStatus}
          data-testid="assistant-status-error"
        />
      )}
      {!status && !statusError && (
        <div className="dim mono" data-testid="assistant-loading">
          {t("common.loading")}
        </div>
      )}

      {status && !status.available && (
        <div
          data-testid="assistant-unavailable"
          data-preset-status={status.preset.status}
        >
          <Panel brk title={t("assistantPage.unavailableTitle")}>
            <div className="note" data-testid="assistant-unavailable-note">
              <span className="i">[i]</span>
              <span>
                {t("assistantPage.unavailableBody", {
                  label: status.preset.label,
                  status: t(`create.system.status.${status.preset.status}`),
                })}{" "}
                {isAdmin
                  ? t("assistantPage.unavailableAdmin")
                  : t("assistantPage.unavailableMember")}
                {status.preset.requirements.length > 0 && (
                  <>
                    {" "}
                    {t("assistantPage.requirements", {
                      list: status.preset.requirements
                        .map((r) =>
                          t(
                            `create.system.requirementCodes.${r.code}`,
                            r.message,
                          ),
                        )
                        .join("; "),
                    })}
                  </>
                )}
              </span>
            </div>
            <div className="assist-actions">
              <Btn
                primary={isAdmin}
                onClick={() => navigate("/create")}
                data-testid="assistant-go-presets"
              >
                {t("assistantPage.goToPresets")}
              </Btn>
            </div>
          </Panel>
        </div>
      )}

      {status?.available && (
        <div className="assist-grid">
          <Panel
            brk
            pad={false}
            title={t("assistantPage.conversations")}
            sub={
              conversation
                ? conversation.title || conversation.id.slice(0, 8)
                : undefined
            }
            end={
              <Btn
                primary
                onClick={() => void newConversation()}
                disabled={busy}
                data-testid="new-conversation"
              >
                {t("assistantPage.newConversation")}
              </Btn>
            }
            style={{ "--i": 0 } as CSSProperties}
          >
            {conversations.length > 0 && (
              <div className="assist-conv-list" data-testid="conversation-list">
                {conversations.slice(0, 8).map((c) => (
                  <button
                    key={c.id}
                    type="button"
                    className={`selchip${conversation?.id === c.id ? " on" : ""}`}
                    onClick={() => selectConversation(c.id)}
                    data-testid={`conversation-${c.id}`}
                    data-selected={conversation?.id === c.id ? "true" : "false"}
                  >
                    {(c.title || c.id.slice(0, 8)).slice(0, 40)}
                    {c.proposal_status && (
                      <span className="dim"> · r{c.proposal_revision}</span>
                    )}
                  </button>
                ))}
              </div>
            )}
            <div
              className="thread assist-thread"
              ref={threadRef}
              data-testid="assistant-thread"
              data-conversation={conversation?.id ?? ""}
            >
              {!conversation && (
                <div className="empty">{t("assistantPage.noConversation")}</div>
              )}
              {conversation && messages.length === 0 && (
                <div className="empty">{t("assistantPage.emptyThread")}</div>
              )}
              {messages.map((msg, i) =>
                msg.role === "user" ? (
                  <div key={i} className="msg user">
                    <div className="who">{t("assistantPage.you")}</div>
                    <div className="bub" style={{ whiteSpace: "pre-wrap" }}>
                      {msg.text}
                    </div>
                  </div>
                ) : msg.role === "assistant" ? (
                  <div
                    key={i}
                    className="msg agent"
                    data-testid={msg.streaming ? "streaming-reply" : undefined}
                  >
                    <div className="who">
                      {t("assistantPage.assistant")}
                      {msg.streaming
                        ? ` · ${t("assistantPage.streaming")}`
                        : ""}
                    </div>
                    <div className="bub">
                      <Markdown
                        text={
                          msg.streaming
                            ? msg.text
                            : stripProposalBlock(msg.text, proposalMarker)
                        }
                      />
                      {msg.streaming && <span className="caret" />}
                    </div>
                  </div>
                ) : msg.role === "tool" ? (
                  <div key={i} className="toolcard">
                    <span className="tc-ic">⇄</span>
                    {msg.name}
                    <Chip tone="good" icon="✓" style={{ marginLeft: "auto" }}>
                      {t("assistantPage.toolCalled")}
                    </Chip>
                  </div>
                ) : (
                  <div
                    key={i}
                    className="note"
                    style={{ borderColor: "var(--crit)" }}
                    data-testid="turn-error"
                  >
                    <span className="i" style={{ color: "var(--crit)" }}>
                      [✕]
                    </span>
                    <span className="mono">{msg.text}</span>
                  </div>
                ),
              )}
            </div>
            <div className="assist-composer">
              <label
                className="mono dim"
                style={{ fontSize: 9.5, letterSpacing: ".18em" }}
                htmlFor="assistant-input"
              >
                {t("assistantPage.composerLabel")}
              </label>
              <textarea
                id="assistant-input"
                className="input mono"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder={t("assistantPage.composerPlaceholder")}
                disabled={!conversation || busy}
                data-testid="assistant-input"
              />
              <div className="row">
                <span className="dim" style={{ fontSize: 11 }}>
                  {t("assistantPage.discussionNote")}
                </span>
                <Btn
                  primary
                  onClick={() => void send()}
                  disabled={!conversation || busy || !input.trim()}
                  data-testid="assistant-send"
                >
                  {busy ? t("assistantPage.sending") : t("assistantPage.send")}
                </Btn>
              </div>
            </div>
            {conversation && (
              <CatalogSummary
                catalog={conversation.catalog}
                onRefresh={() => void refreshCatalog()}
              />
            )}
          </Panel>

          <Panel
            brk
            title={t("assistantPage.proposalTitle")}
            sub={t("assistantPage.proposalSub")}
            end={
              latest ? (
                <>
                  <Chip tone="muted" className="mono">
                    {t("assistantPage.revision", { n: latest.revision })}
                  </Chip>
                  <Chip tone={STATUS_TONE[latest.status]}>
                    {t(`assistantPage.status.${latest.status}`)}
                  </Chip>
                </>
              ) : undefined
            }
            style={{ "--i": 1 } as CSSProperties}
          >
            {!latest && (
              <div className="empty" data-testid="proposal-empty">
                {t("assistantPage.proposalEmpty")}
              </div>
            )}
            {latest && !editing && (
              <ProposalView
                proposal={latest}
                catalog={conversation?.catalog ?? null}
                account={status.account_id}
                region={status.region}
              />
            )}
            {latest && editing && conversation && (
              <ProposalEditor
                draft={editing}
                catalog={conversation.catalog}
                capabilities={status.capabilities}
                errors={editErrors(editing)}
                onChange={setEditing}
              />
            )}
            {latest && (
              <>
                <div
                  className="note"
                  style={{ marginTop: 12 }}
                  data-testid="supported-here"
                >
                  <span className="i">[i]</span>
                  <span>{t("assistantPage.supportedHere")}</span>
                </div>
                {latest.status === "draft" && (
                  <div
                    className="note"
                    style={{ marginTop: 8, borderColor: "var(--amber)" }}
                    data-testid="billable-warning"
                  >
                    <span className="i">[$]</span>
                    <span>
                      {t("assistantPage.billable", {
                        account: status.account_id,
                        region: status.region,
                      })}
                    </span>
                  </div>
                )}
                <div className="assist-actions">
                  {editing ? (
                    <>
                      <Btn
                        onClick={() => setEditing(null)}
                        data-testid="edit-cancel"
                      >
                        {t("assistantPage.cancelEdit")}
                      </Btn>
                      <Btn
                        primary
                        onClick={() => void saveEdit()}
                        data-testid="edit-save"
                      >
                        {t("assistantPage.saveEdit")}
                      </Btn>
                    </>
                  ) : (
                    <>
                      {(latest.status === "draft" ||
                        latest.status === "invalid") && (
                        <Btn
                          onClick={() => setEditing(draftFrom(latest.content))}
                          data-testid="proposal-edit"
                        >
                          {t("assistantPage.edit")}
                        </Btn>
                      )}
                      {(latest.status === "draft" ||
                        latest.status === "invalid") && (
                        <Btn
                          onClick={() =>
                            setConfirm({
                              kind: "reject",
                              revision: latest.revision,
                            })
                          }
                          data-testid="proposal-reject"
                        >
                          {t("assistantPage.reject")}
                        </Btn>
                      )}
                      <span className="spacer" />
                      {latest.status === "draft" && conversation && (
                        <Btn
                          primary
                          disabled={!canDeploy || approving}
                          disabledReason={deployReason}
                          onClick={() =>
                            setConfirm({
                              kind: "approve",
                              pin: {
                                conversationId: conversation.id,
                                revision: latest.revision,
                                hash: latest.content_hash,
                                name: String(latest.content.name ?? ""),
                              },
                            })
                          }
                          data-testid="proposal-approve"
                        >
                          {approving
                            ? t("assistantPage.approving")
                            : t("assistantPage.approve")}
                        </Btn>
                      )}
                    </>
                  )}
                </div>
              </>
            )}
            {approval && shownApproved && (
              <Outcome
                revision={shownApproved.revision}
                approval={approval}
                job={polled.jobId === approval.job_id ? polled.job : null}
                agent={polled.jobId === approval.job_id ? polled.agent : null}
                dateLabel={dateLabel}
              />
            )}
            {olderApprovals.length > 0 && (
              <div className="assist-section" data-testid="outcome-history">
                <h4>{t("assistantPage.outcomeHistory")}</h4>
                <ul className="mono" style={{ fontSize: 11 }}>
                  {olderApprovals.map((p) => (
                    <li
                      key={p.id}
                      data-testid={`outcome-history-${p.revision}`}
                    >
                      {t("assistantPage.revision", { n: p.revision })} ·{" "}
                      {p.approval?.agent_name} ·{" "}
                      {String(p.approval?.agent_status ?? "").toUpperCase()} ·
                      job {(p.approval?.job_id ?? "").slice(0, 8)} ·{" "}
                      {String(p.approval?.job_status ?? "").toUpperCase()}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </Panel>
        </div>
      )}

      <ConfirmDialog
        open={confirm?.kind === "approve"}
        title={t("assistantPage.approveTitle", {
          n: confirm?.kind === "approve" ? confirm.pin.revision : 0,
        })}
        body={t("assistantPage.approveBody", {
          name: confirm?.kind === "approve" ? confirm.pin.name : "",
          account: status?.account_id ?? "",
          region: status?.region ?? "",
        })}
        confirmLabel={t("assistantPage.approveConfirm")}
        onConfirm={() => {
          if (confirm?.kind === "approve") void approve(confirm.pin);
        }}
        onCancel={() => setConfirm(null)}
      />
      <ConfirmDialog
        open={confirm?.kind === "reject"}
        title={t("assistantPage.rejectTitle")}
        body={t("assistantPage.rejectBody", {
          n: confirm?.kind === "reject" ? confirm.revision : 0,
        })}
        confirmLabel={t("assistantPage.rejectConfirm")}
        onConfirm={() => {
          if (confirm?.kind === "reject") void reject(confirm.revision);
        }}
        onCancel={() => setConfirm(null)}
      />
    </section>
  );
}

function CatalogSummary({
  catalog,
  onRefresh,
}: {
  catalog: AssistantCatalog;
  onRefresh: () => void;
}) {
  const { t } = useTranslation();
  const names = (items: { key?: string; kb_id?: string; name?: string }[]) =>
    items.length
      ? items.map((i) => i.name || i.key || i.kb_id).join(", ")
      : t("assistantPage.catalogNone");
  const res = catalog.resources;
  return (
    <div
      className="assist-section"
      style={{ padding: "0 16px 12px" }}
      data-testid="catalog-summary"
    >
      <h4>
        {t("assistantPage.catalogTitle")}{" "}
        <button
          type="button"
          className="selchip"
          style={{ marginLeft: 8, cursor: "pointer" }}
          onClick={onRefresh}
          data-testid="catalog-refresh"
        >
          {t("assistantPage.catalogRefresh")}
        </button>
      </h4>
      <div className="kv">
        <span className="k">{t("assistantPage.catalogTools")}</span>
        <span className="v">
          {names(catalog.tools.filter((x) => x.attachable))}
        </span>
      </div>
      <div className="kv">
        <span className="k">{t("assistantPage.catalogSkills")}</span>
        <span className="v">{names(catalog.skills)}</span>
      </div>
      <div className="kv">
        <span className="k">{t("assistantPage.catalogKbs")}</span>
        <span className="v">
          {names(catalog.knowledge_bases)}
          {res && !(res.kb_gateway_id && res.oauth_provider_arn)
            ? ` · ${t("assistantPage.kbGatewayMissing")}`
            : ""}
        </span>
      </div>
      <div className="kv">
        <span className="k">{t("assistantPage.field.memory")}</span>
        <span className="v">
          {res?.memory_arn
            ? t("assistantPage.memoryWorkspaceAvailable")
            : t("assistantPage.memoryNoShared")}
        </span>
      </div>
      {catalog.warnings.length > 0 && (
        <div className="dim mono" style={{ fontSize: 10.5 }}>
          {t("assistantPage.catalogWarnings", {
            list: catalog.warnings.join("; "),
          })}
        </div>
      )}
    </div>
  );
}

function ProposalView({
  proposal,
  catalog,
  account,
  region,
}: {
  proposal: AssistantProposal;
  catalog: AssistantCatalog | null;
  account: string;
  region: string;
}) {
  const { t } = useTranslation();
  const c = proposal.content;
  const list = (v: unknown): string[] =>
    Array.isArray(v) ? v.map(String) : [];
  const label = (key: string) => {
    const tool = catalog?.tools.find((x) => x.key === key);
    const skill = catalog?.skills.find((x) => x.key === key);
    const kb = catalog?.knowledge_bases.find((x) => x.kb_id === key);
    return (
      tool?.name ??
      skill?.name ??
      (kb ? `${kb.name || kb.kb_id} (${kb.kb_id})` : key)
    );
  };
  const memoryLabel =
    c.memory === "workspace"
      ? t("assistantPage.memoryWorkspace")
      : t("assistantPage.memoryDisabled");
  const chips = (keys: string[], testid: string) =>
    keys.length ? (
      <span className="selchips" data-testid={testid}>
        {keys.map((k) => (
          <span key={k} className="selchip on">
            {label(k)}
          </span>
        ))}
      </span>
    ) : (
      <span className="v">{t("assistantPage.none")}</span>
    );
  const golden = Array.isArray(c.golden_tests) ? c.golden_tests : [];
  const b = proposal.bindings;
  const authLabel = (auth: Record<string, unknown> | null) => {
    const oauth = (auth?.oauth ?? null) as {
      providerArn?: string;
      grantType?: string;
    } | null;
    if (oauth)
      return `oauth · ${oauth.grantType ?? ""} · ${(oauth.providerArn ?? "").split("/").slice(-1)[0]}`;
    return auth ? Object.keys(auth).join(",") : "none";
  };
  return (
    <div
      data-testid="proposal-view"
      data-revision={proposal.revision}
      data-status={proposal.status}
    >
      <div className="dim mono" style={{ fontSize: 10.5, marginBottom: 8 }}>
        {proposal.source === "model"
          ? t("assistantPage.source.model")
          : t("assistantPage.source.member", { who: proposal.created_by })}
      </div>
      {proposal.status === "invalid" && (
        <div
          className="note"
          style={{ borderColor: "var(--crit)", marginBottom: 10 }}
          data-testid="proposal-invalid"
        >
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span>
            {t("assistantPage.invalidNote")}
            <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
              {proposal.validation_errors.map((e, i) => (
                <li key={i} className="mono" style={{ fontSize: 11 }}>
                  {e}
                </li>
              ))}
            </ul>
          </span>
        </div>
      )}
      <div className="kv">
        <span className="k">{t("assistantPage.field.name")}</span>
        <span className="v" data-testid="proposal-name">
          {String(c.name ?? "")}
        </span>
      </div>
      <div className="kv">
        <span className="k">{t("assistantPage.field.model")}</span>
        <span className="v">
          {String(c.model_id ?? "")} · {String(c.model_source ?? "")}
        </span>
      </div>
      <div className="kv">
        <span className="k">{t("assistantPage.field.tools")}</span>
        {chips(list(c.tools), "proposal-tools")}
      </div>
      <div className="kv">
        <span className="k">{t("assistantPage.field.skills")}</span>
        {chips(list(c.skills), "proposal-skills")}
      </div>
      <div className="kv">
        <span className="k">{t("assistantPage.field.kbs")}</span>
        {chips(list(c.knowledge_bases), "proposal-kbs")}
      </div>
      <div className="kv">
        <span className="k">{t("assistantPage.field.memory")}</span>
        <span className="v" data-testid="proposal-memory">
          {memoryLabel}
        </span>
      </div>
      <div className="kv">
        <span className="k">
          {t("assistantPage.field.iterations")} /{" "}
          {t("assistantPage.field.timeout")}
        </span>
        <span className="v">
          {String(c.max_iterations ?? "")} / {String(c.timeout_seconds ?? "")}
        </span>
      </div>
      <div className="kv">
        <span className="k">{t("assistantPage.field.target")}</span>
        <span className="v" data-testid="proposal-target">
          {account} · {region} · harness
        </span>
      </div>
      {b && (
        <div className="assist-section" data-testid="proposal-bindings">
          <h4>{t("assistantPage.bindings")}</h4>
          <ul className="mono" style={{ fontSize: 11 }}>
            {Object.entries(b.resources?.gateways ?? {}).map(([id, g]) => (
              <li key={`gw-${id}`}>
                gateway · {g.gateway_name ?? id} → {g.gateway_arn} ·{" "}
                {t("assistantPage.bindingsAuth")} {authLabel(g.outbound_auth)}
              </li>
            ))}
            {Object.entries(b.resources?.remote_mcp ?? {}).map(([name, m]) => (
              <li key={`mcp-${name}`}>
                mcp · {name} → {m.url}
              </li>
            ))}
            {Object.entries(b.resources?.skills ?? {}).map(([key, s]) => (
              <li key={`skill-${key}`}>
                skill · {key} → {s.path} · {t("assistantPage.bindingsDigest")}{" "}
                {(s.content_digest ?? "").slice(0, 12)}
                {s.object_count != null ? ` (${s.object_count})` : ""}
              </li>
            ))}
            {b.knowledge_bases.map((kb) => (
              <li key={kb.kb_id}>
                kb → {kb.kb_id}
                {kb.name ? ` (${kb.name})` : ""}
                {b.resources?.kb_gateway
                  ? ` · via ${b.resources.kb_gateway.gateway_id}`
                  : ""}
              </li>
            ))}
            <li data-testid="binding-memory">
              memory → {b.resources?.memory.mode}
              {b.resources?.memory.arn ? ` · ${b.resources.memory.arn}` : ""}
            </li>
          </ul>
        </div>
      )}
      <div className="assist-section">
        <h4>{t("assistantPage.field.prompt")}</h4>
        <div className="assist-prompt" data-testid="proposal-prompt">
          {String(c.system_prompt ?? "")}
        </div>
      </div>
      {typeof c.summary === "string" && c.summary && (
        <div className="assist-section">
          <h4>{t("assistantPage.summary")}</h4>
          <p style={{ margin: 0, fontSize: 12.5 }}>{c.summary}</p>
        </div>
      )}
      {list(c.requirements_baseline).length > 0 && (
        <div className="assist-section">
          <h4>{t("assistantPage.baseline")}</h4>
          <ul>
            {list(c.requirements_baseline).map((x, i) => (
              <li key={i}>{x}</li>
            ))}
          </ul>
        </div>
      )}
      {list(c.assumptions).length > 0 && (
        <div className="assist-section">
          <h4>{t("assistantPage.assumptions")}</h4>
          <ul>
            {list(c.assumptions).map((x, i) => (
              <li key={i}>{x}</li>
            ))}
          </ul>
        </div>
      )}
      {list(c.manual_tasks).length > 0 && (
        <div className="assist-section" data-testid="manual-tasks">
          <h4>{t("assistantPage.manualTasks")}</h4>
          <ul>
            {list(c.manual_tasks).map((x, i) => (
              <li key={i}>{x}</li>
            ))}
          </ul>
        </div>
      )}
      {golden.length > 0 && (
        <div className="assist-section" data-testid="golden-tests">
          <h4>{t("assistantPage.goldenTests")}</h4>
          <table className="assist-gt">
            <thead>
              <tr>
                <th>{t("assistantPage.gt.id")}</th>
                <th>{t("assistantPage.gt.input")}</th>
                <th>{t("assistantPage.gt.expected")}</th>
                <th>{t("assistantPage.gt.forbidden")}</th>
                <th>{t("assistantPage.gt.evaluator")}</th>
                <th>{t("assistantPage.gt.source")}</th>
              </tr>
            </thead>
            <tbody>
              {golden.map((g) => (
                <tr key={g.id}>
                  <td className="mono">{g.id}</td>
                  <td>{g.input}</td>
                  <td>{g.expected_response ?? ""}</td>
                  <td>{g.forbidden_behavior ?? ""}</td>
                  <td className="mono">{g.evaluator ?? ""}</td>
                  <td className="mono">{g.source ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {list(c.evaluator_recommendations).length > 0 && (
        <div className="assist-section">
          <h4>{t("assistantPage.evaluators")}</h4>
          <ul>
            {list(c.evaluator_recommendations).map((x, i) => (
              <li key={i} className="mono">
                {x}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ProposalEditor({
  draft,
  catalog,
  capabilities,
  errors,
  onChange,
}: {
  draft: EditDraft;
  catalog: AssistantCatalog;
  capabilities: AssistantStatus["capabilities"];
  errors: Record<string, boolean>;
  onChange: (next: EditDraft) => void;
}) {
  const { t } = useTranslation();
  const set = <K extends keyof EditDraft>(key: K, value: EditDraft[K]) =>
    onChange({ ...draft, [key]: value });
  const toggle = (
    key: "tools" | "skills" | "knowledge_bases",
    value: string,
  ) => {
    const has = draft[key].includes(value);
    set(
      key,
      has ? draft[key].filter((x) => x !== value) : [...draft[key], value],
    );
  };
  const bad = (k: string): CSSProperties | undefined =>
    errors[k] ? { borderColor: "var(--crit)" } : undefined;
  const models = MODEL_CATALOG[draft.model_source];
  const memoryModes: AssistantMemoryMode[] = ["disabled", "workspace"];
  return (
    <div data-testid="proposal-editor">
      <div className="dim" style={{ fontSize: 11, marginBottom: 10 }}>
        {t("assistantPage.editHint")}
      </div>
      <div className="field">
        <label htmlFor="pe-name">{t("assistantPage.field.name")}</label>
        <input
          id="pe-name"
          className="input mono"
          style={bad("name")}
          value={draft.name}
          onChange={(e) => set("name", e.target.value)}
          data-testid="edit-name"
        />
      </div>
      <div className="field">
        <label htmlFor="pe-source">
          {t("assistantPage.field.modelSource")}
        </label>
        <select
          id="pe-source"
          className="input"
          value={draft.model_source}
          onChange={(e) => {
            const source = e.target.value as ModelSource;
            onChange({
              ...draft,
              model_source: source,
              model_id: MODEL_CATALOG[source][0].model_id,
            });
          }}
        >
          <option value="bedrock">bedrock</option>
          <option value="mantle">mantle</option>
        </select>
      </div>
      <div className="field">
        <label htmlFor="pe-model">{t("assistantPage.field.model")}</label>
        <input
          id="pe-model"
          className="input mono"
          style={bad("model_id")}
          value={draft.model_id}
          list="pe-models"
          onChange={(e) => set("model_id", e.target.value)}
          data-testid="edit-model"
        />
        <datalist id="pe-models">
          {models.map((m) => (
            <option key={m.model_id} value={m.model_id}>
              {m.label}
            </option>
          ))}
        </datalist>
      </div>
      <div className="field">
        <label htmlFor="pe-prompt">{t("assistantPage.field.prompt")}</label>
        <textarea
          id="pe-prompt"
          className="input mono"
          style={{ minHeight: 140, ...bad("system_prompt") }}
          value={draft.system_prompt}
          onChange={(e) => set("system_prompt", e.target.value)}
          data-testid="edit-prompt"
        />
      </div>
      <div className="field">
        <label>{t("assistantPage.field.tools")}</label>
        <div className="selchips">
          {catalog.tools
            .filter((x) => x.attachable)
            .map((x) => (
              <button
                key={x.key}
                type="button"
                className={`selchip${draft.tools.includes(x.key) ? " on" : ""}`}
                onClick={() => toggle("tools", x.key)}
                data-testid={`edit-tool-${x.name}`}
              >
                {x.name}
              </button>
            ))}
          {catalog.tools.length === 0 && (
            <span className="dim mono">{t("assistantPage.catalogNone")}</span>
          )}
        </div>
      </div>
      <div className="field">
        <label>{t("assistantPage.field.skills")}</label>
        <div className="selchips">
          {catalog.skills.map((x) => (
            <button
              key={x.key}
              type="button"
              className={`selchip${draft.skills.includes(x.key) ? " on" : ""}`}
              onClick={() => toggle("skills", x.key)}
            >
              {x.name}
            </button>
          ))}
          {catalog.skills.length === 0 && (
            <span className="dim mono">{t("assistantPage.catalogNone")}</span>
          )}
        </div>
      </div>
      <div className="field">
        <label>{t("assistantPage.field.kbs")}</label>
        <div className="selchips">
          {catalog.knowledge_bases.map((x) => (
            <button
              key={x.kb_id}
              type="button"
              className={`selchip${draft.knowledge_bases.includes(x.kb_id) ? " on" : ""}`}
              onClick={() => toggle("knowledge_bases", x.kb_id)}
              disabled={!capabilities.kb_gateway}
              title={
                capabilities.kb_gateway
                  ? undefined
                  : t("assistantPage.kbGatewayMissing")
              }
            >
              {x.name || x.kb_id}
            </button>
          ))}
          {catalog.knowledge_bases.length === 0 && (
            <span className="dim mono">{t("assistantPage.catalogNone")}</span>
          )}
        </div>
      </div>
      <div className="field">
        <label>{t("assistantPage.field.memory")}</label>
        <div className="selchips">
          {memoryModes.map((mode) => (
            <button
              key={mode}
              type="button"
              className={`selchip${draft.memory === mode ? " on" : ""}`}
              onClick={() => set("memory", mode)}
              disabled={mode === "workspace" && !capabilities.shared_memory}
              title={
                mode === "workspace" && !capabilities.shared_memory
                  ? t("assistantPage.memoryNoShared")
                  : undefined
              }
              data-testid={`edit-memory-${mode}`}
            >
              {mode === "workspace"
                ? t("assistantPage.memoryWorkspace")
                : t("assistantPage.memoryDisabled")}
            </button>
          ))}
        </div>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
        <div className="field">
          <label htmlFor="pe-iter">{t("assistantPage.field.iterations")}</label>
          <input
            id="pe-iter"
            className="input mono"
            type="number"
            min={1}
            max={100}
            style={bad("max_iterations")}
            value={draft.max_iterations}
            onChange={(e) => set("max_iterations", Number(e.target.value))}
          />
        </div>
        <div className="field">
          <label htmlFor="pe-timeout">{t("assistantPage.field.timeout")}</label>
          <input
            id="pe-timeout"
            className="input mono"
            type="number"
            min={10}
            max={3600}
            style={bad("timeout_seconds")}
            value={draft.timeout_seconds}
            onChange={(e) => set("timeout_seconds", Number(e.target.value))}
          />
        </div>
      </div>
    </div>
  );
}

function Outcome({
  revision,
  approval,
  job,
  agent,
  dateLabel,
}: {
  revision: number;
  approval: AssistantApproval;
  job: JobInfo | null;
  agent: AgentInfo | null;
  dateLabel: (iso: string | null) => string;
}) {
  const { t } = useTranslation();
  const jobStatus = job?.status ?? approval.job_status;
  const agentStatus = agent?.status ?? approval.agent_status;
  const failed = jobStatus === "failed" || agentStatus === "failed";
  const succeeded = jobStatus === "succeeded" && agentStatus === "active";
  const stages: StageInfo[] =
    agent?.deployments?.find((d) => d.job_id === approval.job_id)?.stages ?? [];
  const tone: ChipTone = failed ? "crit" : succeeded ? "good" : "warn";
  return (
    <div
      className="assist-section assist-outcome"
      data-testid="deploy-outcome"
      data-job-status={jobStatus ?? ""}
      data-revision={revision}
      data-job={approval.job_id ?? ""}
    >
      <h4>
        {t("assistantPage.outcomeTitle")} ·{" "}
        {t("assistantPage.revision", { n: revision })}
      </h4>
      <div className="dim mono" style={{ fontSize: 10.5 }}>
        {t("assistantPage.outcomeApprovedBy", {
          who: approval.approved_by ?? "",
          at: dateLabel(approval.approved_at),
        })}
      </div>
      <div className="kv">
        <span className="k">{t("assistantPage.outcomeAgent")}</span>
        <span className="v" data-testid="outcome-agent">
          {approval.agent_name ?? agent?.name ?? ""} ·{" "}
          <Chip tone={tone}>{String(agentStatus ?? "").toUpperCase()}</Chip>
        </span>
      </div>
      <div className="kv">
        <span className="k">{t("assistantPage.outcomeJob")}</span>
        <span className="v" data-testid="outcome-job">
          {(approval.job_id ?? "").slice(0, 8)} ·{" "}
          {String(jobStatus ?? "").toUpperCase()}
        </span>
      </div>
      {stages.length > 0 && (
        <div className="assist-stages" data-testid="deploy-stages">
          {stages.map((s) => (
            <Chip
              key={s.name}
              tone={
                s.status === "failed"
                  ? "crit"
                  : s.status === "succeeded"
                    ? "good"
                    : s.status === "running"
                      ? "warn"
                      : "muted"
              }
              className="mono"
            >
              {s.name} · {t(`assistantPage.stage.${s.status}`)}
            </Chip>
          ))}
        </div>
      )}
      <div
        className={failed ? "note" : "dim"}
        style={failed ? { borderColor: "var(--crit)" } : { fontSize: 12 }}
        data-testid="deploy-verdict"
      >
        {failed && (
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
        )}
        <span>
          {failed
            ? t("assistantPage.outcomeFailed")
            : succeeded
              ? t("assistantPage.outcomeSucceeded")
              : t("assistantPage.outcomeRunning")}
          {failed && (agent?.error || approval.agent_error || job?.error) && (
            <>
              {" "}
              <span className="mono" style={{ fontSize: 11 }}>
                {agent?.error || approval.agent_error || job?.error}
              </span>
            </>
          )}
        </span>
      </div>
      <div className="assist-actions">
        {approval.agent_id && (
          <Link className="assist-link" to="/create" data-testid="open-agent">
            {t("assistantPage.openAgent")}
          </Link>
        )}
        {succeeded && approval.agent_id && (
          <Link
            className="assist-link"
            to={`/chat?agent=${encodeURIComponent(approval.agent_id)}`}
            data-testid="open-chat"
          >
            {t("assistantPage.openChat")}
          </Link>
        )}
      </div>
    </div>
  );
}

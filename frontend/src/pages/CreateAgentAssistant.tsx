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
  AssistantMessage,
  AssistantProposal,
  AssistantProposalContent,
  AssistantProposalStatus,
  AssistantStatus,
  JobInfo,
  StageInfo,
} from "../lib/api";
import { api, ApiError, errorMessage } from "../lib/api";
import { MODEL_CATALOG, type ModelSource } from "../lib/models";
import { useWorkspace } from "../workspace/workspace-context";

/**
 * Architect assistant (SE-039): a member conversation with the protected
 * `aws-agent-solution-architect` preset that ends in an inert, reviewable proposal
 * for ONE new managed Harness. Discussion never writes AWS; the proposal is shown
 * verbatim; only APPROVE & DEPLOY (a separate authenticated call naming the exact
 * revision + content hash) creates the agent through the normal deploy job.
 *
 * Every asynchronous outcome is checked against the workspace it started in and
 * the mounted state, so a stream, poll or approval finishing after a workspace
 * switch or navigation never lands in the wrong context.
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

interface LiveMessage {
  role: "user" | "assistant" | "tool" | "error";
  text: string;
  name?: string | null;
  streaming?: boolean;
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
  const memory =
    (content.memory as { short_term?: boolean; long_term?: boolean }) ?? {};
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
    memory: {
      short_term: memory.short_term ?? true,
      long_term: memory.long_term ?? false,
    },
    max_iterations: Number(content.max_iterations ?? 10),
    timeout_seconds: Number(content.timeout_seconds ?? 300),
  };
}

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
  const [confirm, setConfirm] = useState<"approve" | "reject" | null>(null);
  const [job, setJob] = useState<JobInfo | null>(null);
  const [agent, setAgent] = useState<AgentInfo | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // stale-outcome guard: unmounted or a different workspace ⇒ ignore the result
  const alive = useRef(true);
  const scope = useRef(workspaceId);
  useEffect(() => {
    alive.current = true;
    scope.current = workspaceId;
    return () => {
      alive.current = false;
      abortRef.current?.abort();
    };
  }, [workspaceId]);
  const stillCurrent = useCallback(
    (startedIn: string | null) => alive.current && scope.current === startedIn,
    [],
  );
  const apiMessage = useCallback(
    (err: unknown) =>
      err instanceof ApiError
        ? t(`apiErrors.${err.code}`, err.message)
        : errorMessage(err),
    [t],
  );
  const unauthorized = (err: unknown) =>
    err instanceof ApiError &&
    (err.code === "http.401" ||
      err.code === "http.403" ||
      err.code === "workspace.forbidden" ||
      err.code === "auth.permission_required");

  const loadStatus = useCallback(() => {
    const startedIn = scope.current;
    setStatusError(null);
    void api
      .assistantStatus()
      .then((res) => {
        if (!stillCurrent(startedIn)) return;
        setStatus(res);
      })
      .catch((err: unknown) => {
        if (!stillCurrent(startedIn)) return;
        setStatusError(apiMessage(err));
      });
  }, [apiMessage, stillCurrent]);

  const loadConversations = useCallback(() => {
    const startedIn = scope.current;
    void api
      .assistantConversations()
      .then((res) => {
        if (stillCurrent(startedIn)) setConversations(res.conversations);
      })
      .catch(() => {
        /* the list is secondary; the status panel reports the real failure */
      });
  }, [stillCurrent]);

  const openConversation = useCallback(
    (id: string) => {
      const startedIn = scope.current;
      void api
        .assistantConversation(id)
        .then((detail) => {
          if (!stillCurrent(startedIn)) return;
          setConversation(detail);
          setMessages(toLive(detail.messages));
          setEditing(null);
          setSearchParams(
            (prev) => {
              const next = new URLSearchParams(prev);
              next.set("conversation", id);
              return next;
            },
            { replace: true },
          );
        })
        .catch((err: unknown) => {
          if (!stillCurrent(startedIn)) return;
          toast(apiMessage(err));
          if (
            err instanceof ApiError &&
            err.code === "assistant.conversation_not_found"
          ) {
            setSearchParams(
              (prev) => {
                const next = new URLSearchParams(prev);
                next.delete("conversation");
                return next;
              },
              { replace: true },
            );
          }
        });
    },
    [apiMessage, setSearchParams, stillCurrent, toast],
  );

  // A workspace switch remounts the routed subtree, so component state (draft
  // input, open conversation) is reset by construction; the reads re-run here.
  useEffect(() => {
    if (workspaceId === null) return;
    setConversation(null);
    setMessages([]);
    setJob(null);
    setAgent(null);
    loadStatus();
    loadConversations();
  }, [loadConversations, loadStatus, workspaceId]);

  useEffect(() => {
    if (workspaceId === null || !status?.available || !linkedConversation)
      return;
    if (conversation?.id === linkedConversation) return;
    openConversation(linkedConversation);
  }, [
    conversation?.id,
    linkedConversation,
    openConversation,
    status?.available,
    workspaceId,
  ]);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [messages]);

  const latest = useMemo(() => {
    const rows = conversation?.proposals ?? [];
    return rows.length ? rows[rows.length - 1] : null;
  }, [conversation]);
  const approvedRevision = useMemo(
    () =>
      (conversation?.proposals ?? []).find((p) => p.status === "approved") ??
      null,
    [conversation],
  );
  const approval: AssistantApproval | null = approvedRevision?.approval ?? null;

  // deployment outcome: poll the ordinary job + agent while the job is live
  useEffect(() => {
    if (!approval?.job_id || !approval.agent_id) {
      setJob(null);
      setAgent(null);
      return;
    }
    const startedIn = scope.current;
    const jobId = approval.job_id;
    const agentId = approval.agent_id;
    let timer: number | undefined;
    const tick = () => {
      void Promise.all([api.getJob(jobId), api.getAgent(agentId)])
        .then(([j, a]) => {
          if (!stillCurrent(startedIn)) return;
          setJob(j);
          setAgent(a);
          if (j.status === "queued" || j.status === "running") {
            timer = window.setTimeout(tick, JOB_POLL_MS);
          }
        })
        .catch(() => {
          if (!stillCurrent(startedIn)) return;
          timer = window.setTimeout(tick, JOB_POLL_MS * 2);
        });
    };
    tick();
    return () => {
      if (timer) window.clearTimeout(timer);
    };
  }, [approval?.agent_id, approval?.job_id, stillCurrent]);

  const newConversation = async () => {
    const startedIn = scope.current;
    setBusy(true);
    try {
      const detail = await api.assistantCreateConversation();
      if (!stillCurrent(startedIn)) return;
      setConversation(detail);
      setMessages([]);
      setEditing(null);
      setConversations((prev) => [
        {
          ...detail,
          catalog: undefined,
          messages: undefined,
          proposals: undefined,
        } as never,
        ...prev,
      ]);
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set("conversation", detail.id);
          return next;
        },
        { replace: true },
      );
      if (detail.catalog.warnings.length) {
        toast(
          t("assistantPage.catalogWarnings", {
            list: detail.catalog.warnings.join("; "),
          }),
        );
      }
    } catch (err) {
      if (!stillCurrent(startedIn)) return;
      toast(apiMessage(err));
      if (err instanceof ApiError && err.code === "assistant.unavailable")
        loadStatus();
    } finally {
      if (stillCurrent(startedIn)) setBusy(false);
    }
  };

  const send = async () => {
    if (!conversation || !input.trim() || busy) return;
    const startedIn = scope.current;
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
        if (!stillCurrent(startedIn)) return;
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
          setMessages((m) => [
            ...m.filter(
              (x) => !(x.role === "assistant" && x.streaming && !x.text),
            ),
            { role: "error", text: String(data.message ?? "") },
          ]);
        }
      }
      if (!stillCurrent(startedIn)) return;
      setMessages((m) => m.map((x) => ({ ...x, streaming: false })));
      // re-read the server's transcript + proposals: the ledger is the truth
      const detail = await api.assistantConversation(conversationId);
      if (!stillCurrent(startedIn)) return;
      setConversation(detail);
      setMessages(toLive(detail.messages));
      if (proposal) setEditing(null);
      loadConversations();
    } catch (err) {
      if (!stillCurrent(startedIn) || controller.signal.aborted) return;
      setMessages((m) => [
        ...m.filter((x) => !(x.role === "assistant" && x.streaming && !x.text)),
        {
          role: "error",
          text: t("assistantPage.turnFailed", { msg: apiMessage(err) }),
        },
      ]);
      if (unauthorized(err)) toast(t("assistantPage.expiredSession"));
      else if (err instanceof ApiError && err.code === "assistant.unavailable")
        loadStatus();
    } finally {
      if (stillCurrent(startedIn)) setBusy(false);
      if (abortRef.current === controller) abortRef.current = null;
    }
  };

  const refreshCatalog = async () => {
    if (!conversation) return;
    const startedIn = scope.current;
    try {
      const res = await api.assistantRefreshCatalog(conversation.id);
      if (!stillCurrent(startedIn)) return;
      setConversation((c) => (c ? { ...c, catalog: res.catalog } : c));
      toast(t("assistantPage.catalogChanged"), "good");
    } catch (err) {
      if (stillCurrent(startedIn)) toast(apiMessage(err));
    }
  };

  const reload = async (conversationId: string, startedIn: string | null) => {
    const detail = await api.assistantConversation(conversationId);
    if (!stillCurrent(startedIn)) return null;
    setConversation(detail);
    setMessages(toLive(detail.messages));
    loadConversations();
    return detail;
  };

  const approve = async () => {
    if (!conversation || !latest) return;
    const startedIn = scope.current;
    setConfirm(null);
    setApproving(true);
    try {
      const res = await api.assistantApproveProposal(
        conversation.id,
        latest.revision,
        latest.content_hash,
      );
      if (!stillCurrent(startedIn)) return;
      toast(
        res.started
          ? t("assistantPage.approvedToast", {
              job: (res.job_id ?? "").slice(0, 8),
            })
          : t("assistantPage.alreadyApprovedToast"),
        "good",
      );
      await reload(conversation.id, startedIn);
    } catch (err) {
      if (!stillCurrent(startedIn)) return;
      toast(apiMessage(err));
      if (unauthorized(err)) loadStatus();
      else await reload(conversation.id, startedIn).catch(() => null);
    } finally {
      if (stillCurrent(startedIn)) setApproving(false);
    }
  };

  const reject = async () => {
    if (!conversation || !latest) return;
    const startedIn = scope.current;
    setConfirm(null);
    try {
      await api.assistantRejectProposal(conversation.id, latest.revision);
      if (!stillCurrent(startedIn)) return;
      toast(t("assistantPage.rejectedToast"), "good");
      setEditing(null);
      await reload(conversation.id, startedIn);
    } catch (err) {
      if (stillCurrent(startedIn)) toast(apiMessage(err));
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
      const res = await api.assistantEditProposal(conversation.id, content);
      if (!stillCurrent(startedIn)) return;
      toast(
        t("assistantPage.editSavedToast", { n: res.proposal.revision }),
        "good",
      );
      setEditing(null);
      await reload(conversation.id, startedIn);
    } catch (err) {
      if (stillCurrent(startedIn)) toast(apiMessage(err));
    }
  };

  const canDeploy =
    (status?.can_deploy ?? can("agents.deploy")) &&
    status?.deploy_requirements.length === 0;
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
                    onClick={() => openConversation(c.id)}
                    data-testid={`conversation-${c.id}`}
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
                  <div key={i} className="msg agent">
                    <div className="who">
                      {t("assistantPage.assistant")}
                      {msg.streaming
                        ? ` · ${t("assistantPage.streaming")}`
                        : ""}
                    </div>
                    <div className="bub">
                      <Markdown text={msg.text} />
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
                          onClick={() => setConfirm("reject")}
                          data-testid="proposal-reject"
                        >
                          {t("assistantPage.reject")}
                        </Btn>
                      )}
                      <span className="spacer" />
                      {latest.status === "draft" && (
                        <Btn
                          primary
                          disabled={!canDeploy || approving}
                          disabledReason={deployReason}
                          onClick={() => setConfirm("approve")}
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
            {approval && (
              <Outcome
                approval={approval}
                job={job}
                agent={agent}
                dateLabel={dateLabel}
              />
            )}
          </Panel>
        </div>
      )}

      <ConfirmDialog
        open={confirm === "approve" && !!latest}
        title={t("assistantPage.approveTitle", { n: latest?.revision ?? 0 })}
        body={t("assistantPage.approveBody", {
          name: String(latest?.content.name ?? ""),
          account: status?.account_id ?? "",
          region: status?.region ?? "",
        })}
        confirmLabel={t("assistantPage.approveConfirm")}
        onConfirm={() => void approve()}
        onCancel={() => setConfirm(null)}
      />
      <ConfirmDialog
        open={confirm === "reject" && !!latest}
        title={t("assistantPage.rejectTitle")}
        body={t("assistantPage.rejectBody", { n: latest?.revision ?? 0 })}
        confirmLabel={t("assistantPage.rejectConfirm")}
        onConfirm={() => void reject()}
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
        <span className="v">{names(catalog.knowledge_bases)}</span>
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
  const memory =
    (c.memory as { short_term?: boolean; long_term?: boolean } | undefined) ??
    {};
  const memoryLabel =
    [
      memory.short_term ? t("assistantPage.memoryShort") : null,
      memory.long_term ? t("assistantPage.memoryLong") : null,
    ]
      .filter(Boolean)
      .join(" + ") || t("assistantPage.memoryOff");
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
        <span className="v">{memoryLabel}</span>
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
      {proposal.bindings && (
        <div className="assist-section" data-testid="proposal-bindings">
          <h4>{t("assistantPage.bindings")}</h4>
          <ul className="mono" style={{ fontSize: 11 }}>
            {proposal.bindings.tools.map((tool) => (
              <li key={`${tool.type}:${tool.name}`}>
                {tool.type} · {tool.name} →{" "}
                {tool.type === "mcp"
                  ? tool.config.url
                  : `record ${tool.config.record_id} · gateway ${tool.config.gateway_id}`}
              </li>
            ))}
            {proposal.bindings.skills.map((path) => (
              <li key={path}>skill → {path}</li>
            ))}
            {proposal.bindings.knowledge_bases.map((kb) => (
              <li key={kb.kb_id}>
                kb → {kb.kb_id}
                {kb.name ? ` (${kb.name})` : ""}
              </li>
            ))}
            {proposal.bindings.tools.length +
              proposal.bindings.skills.length +
              proposal.bindings.knowledge_bases.length ===
              0 && <li>{t("assistantPage.none")}</li>}
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
  errors,
  onChange,
}: {
  draft: EditDraft;
  catalog: AssistantCatalog;
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
          <button
            type="button"
            className={`selchip${draft.memory.short_term ? " on" : ""}`}
            onClick={() =>
              set("memory", {
                ...draft.memory,
                short_term: !draft.memory.short_term,
              })
            }
          >
            {t("assistantPage.memoryShort")}
          </button>
          <button
            type="button"
            className={`selchip${draft.memory.long_term ? " on" : ""}`}
            onClick={() =>
              set("memory", {
                ...draft.memory,
                long_term: !draft.memory.long_term,
              })
            }
          >
            {t("assistantPage.memoryLong")}
          </button>
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
  approval,
  job,
  agent,
  dateLabel,
}: {
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
    >
      <h4>{t("assistantPage.outcomeTitle")}</h4>
      <div className="dim mono" style={{ fontSize: 10.5 }}>
        {t("assistantPage.outcomeApprovedBy", {
          who: approval.approved_by ?? "",
          at: dateLabel(approval.approved_at),
        })}
      </div>
      <div className="kv">
        <span className="k">{t("assistantPage.outcomeAgent")}</span>
        <span className="v">
          {approval.agent_name ?? agent?.name ?? ""} ·{" "}
          <Chip tone={tone}>{String(agentStatus ?? "").toUpperCase()}</Chip>
        </span>
      </div>
      <div className="kv">
        <span className="k">{t("assistantPage.outcomeJob")}</span>
        <span className="v">
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
          <Link
            className="assist-link"
            style={{ marginTop: 0 }}
            to="/create"
            data-testid="open-agent"
          >
            {t("assistantPage.openAgent")}
          </Link>
        )}
        {succeeded && approval.agent_id && (
          <Link
            className="assist-link"
            style={{ marginTop: 0 }}
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

import { Plus } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import {
  type AgentInfo,
  api,
  ApiError,
  type AssistantApproval,
  type AssistantConversationDetail,
  type AssistantProposal,
  type AssistantStatus,
  type AssistantTurnRequest,
  type JobInfo,
} from "../../../lib/api";
import {
  ASSISTANT_JOB_POLL_MS,
  type AssistantLiveMessage,
  isAssistantUnauthorized,
  openAssistantTurn,
  proposalContentFromDraft,
  proposalEditErrors,
  proposalEditStart,
  type ProposalEditDraft,
  sseEvents,
  toLiveMessages,
} from "../../../lib/assistant";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, FlowHeader, Spin, Tag } from "../../ui";
import { PROPOSAL_TONE, SECTION_IDS, shortId, useApiMessage } from "./common";
import { COMPOSER_ID, DiscussionCard } from "./Discussion";
import { EvalAssetsCard } from "./EvalAssets";
import { PreparationCard } from "./Preparation";
import { AdlcCard, CreationProgressCard } from "./Progress";
import { Outcome, ProposalEditor, ProposalView } from "./Proposal";
import { useClearConversation } from "./useClearConversation";

/** What the approve dialog was opened on — submitted verbatim, never "the latest". */
interface PinnedApproval {
  conversationId: string;
  revision: number;
  hash: string;
  name: string;
}

/**
 * One architect conversation (`?view=detail&id=`): discussion → resource
 * preparation → reviewable proposal → APPROVE & DEPLOY → evaluation assets → next
 * steps. The component is keyed by (workspace, conversation), so selecting another
 * conversation or switching workspace remounts it: every in-flight load, stream and
 * poll of the previous one is dropped (`alive`) and its stream aborted.
 */
export function AssistantDetail({
  id, status, workspaceId, onUnavailable,
}: {
  id: string;
  status: AssistantStatus;
  workspaceId: string;
  onUnavailable: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const apiMessage = useApiMessage();
  const [, setParams] = useSearchParams();
  const { can } = useAuth();

  const [conversation, setConversation] = useState<AssistantConversationDetail | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [messages, setMessages] = useState<AssistantLiveMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [approving, setApproving] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [resourcesDirty, setResourcesDirty] = useState(false);
  const [editing, setEditing] = useState<ProposalEditDraft | null>(null);
  const [confirm, setConfirm] = useState<
    { kind: "approve"; pin: PinnedApproval } | { kind: "reject"; revision: number } | null
  >(null);
  const [polled, setPolled] = useState<{ jobId: string; job: JobInfo | null; agent: AgentInfo | null }>({
    jobId: "", job: null, agent: null,
  });
  const threadRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const alive = useRef(true);
  const tRef = useRef(t);
  tRef.current = t;
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, []);

  const back = useCallback(() => setParams({}), [setParams]);
  const clear = useClearConversation(back);

  // load the conversation (read-only)
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let live = true;
    setLoadError(null);
    api.assistantConversation(id, workspaceId).then(
      (detail) => {
        if (!live || !alive.current) return;
        setConversation(detail);
        setMessages(toLiveMessages(detail.messages));
      },
      (err: unknown) => {
        if (!live || !alive.current) return;
        if (err instanceof ApiError && err.code === "assistant.conversation_not_found") {
          toast("error", apiMessage(err));
          back();
          return;
        }
        setLoadError(apiMessage(err));
      },
    );
    return () => { live = false; };
  }, [id, workspaceId, nonce, apiMessage, back, toast]);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [messages]);

  const proposals = useMemo(() => conversation?.proposals ?? [], [conversation]);
  const latest = proposals.length ? proposals[proposals.length - 1] : null;
  const approvedRevisions = useMemo(() => proposals.filter((p) => p.status === "approved" && p.approval), [proposals]);
  // The outcome shown belongs to the latest revision when that one was approved,
  // otherwise to the most recent approved revision; older approvals are listed.
  const shownApproved = latest?.status === "approved"
    ? latest
    : approvedRevisions.length ? approvedRevisions[approvedRevisions.length - 1] : null;
  const approval: AssistantApproval | null = shownApproved?.approval ?? null;
  const resourcesLocked = approvedRevisions.length > 0;
  const olderApprovals = approvedRevisions.filter((p) => p.id !== shownApproved?.id);
  const deployed = useMemo(() => {
    if (!approval) return null;
    const live = polled.jobId === approval.job_id;
    return {
      agentId: approval.agent_id,
      agentName: approval.agent_name ?? (live ? polled.agent?.name : null) ?? null,
      agentStatus: (live ? polled.agent?.status : null) ?? approval.agent_status,
      jobStatus: (live ? polled.job?.status : null) ?? approval.job_status,
    };
  }, [approval, polled]);

  // A dialog opened on revision N is invalidated the moment the latest revision or
  // its hash changes: never approve a different review silently.
  useEffect(() => {
    if (!confirm || confirm.kind !== "approve") return;
    if (
      !latest || conversation?.id !== confirm.pin.conversationId || latest.revision !== confirm.pin.revision
      || latest.content_hash !== confirm.pin.hash || latest.status !== "draft"
    ) {
      setConfirm(null);
      toast("error", tRef.current("assistantPage.dialogStale"));
    }
  }, [confirm, conversation?.id, latest, toast]);

  // Deployment outcome: poll the ordinary job + agent while the job is live.
  useEffect(() => {
    const jobId = approval?.job_id;
    const agentId = approval?.agent_id;
    if (!jobId || !agentId) {
      setPolled({ jobId: "", job: null, agent: null });
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    const tick = () => {
      void Promise.all([api.getJob(jobId), api.getAgent(agentId)])
        .then(([j, a]) => {
          if (cancelled || !alive.current) return;
          setPolled({ jobId, job: j, agent: a });
          if (j.status === "queued" || j.status === "running") timer = window.setTimeout(tick, ASSISTANT_JOB_POLL_MS);
        })
        .catch(() => {
          if (cancelled || !alive.current) return;
          timer = window.setTimeout(tick, ASSISTANT_JOB_POLL_MS * 2);
        });
    };
    tick();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [approval?.agent_id, approval?.job_id]);

  const reload = async (conversationId: string) => {
    const detail = await api.assistantConversation(conversationId, workspaceId);
    if (!alive.current || detail.id !== conversationId) return null;
    setConversation(detail);
    setMessages(toLiveMessages(detail.messages));
    return detail;
  };

  const newConversation = async () => {
    setCreating(true);
    try {
      const detail = await api.assistantCreateConversation();
      if (detail.catalog.warnings.length) {
        toast("error", t("assistantPage.catalogWarnings", { list: detail.catalog.warnings.join("; ") }));
      }
      setParams({ view: "detail", id: detail.id });
    } catch (err) {
      toast("error", apiMessage(err));
      if (err instanceof ApiError && err.code === "assistant.unavailable") onUnavailable();
    } finally {
      if (alive.current) setCreating(false);
    }
  };

  const repairDisabledReason = !conversation
    ? t("assistantEval.repairUnavailable")
    : editing
      ? t("assistantEval.repairProposalEditorOpen")
      : busy || approving || confirm || clear.busy || conversation.turn_in_progress !== null
        ? t("assistantEval.repairBusy")
        : undefined;

  // Both the composer and evaluation repair own the same stream. The controller is
  // also a synchronous claim, before React renders `busy`.
  const send = async (request: AssistantTurnRequest): Promise<AssistantProposal | null> => {
    const repairing = !!request.evaluation_plan_repair;
    if (
      !conversation || !request.prompt.trim() || busy || preparing || abortRef.current
      || conversation.turn_in_progress !== null || (repairing && repairDisabledReason)
    ) {
      if (repairing) throw new Error(repairDisabledReason ?? t("assistantEval.repairBusy"));
      return null;
    }
    const conversationId = conversation.id;
    const { prompt } = request;
    if (!repairing) setInput("");
    setBusy(true);
    setMessages((m) => [...m, { role: "user", text: prompt }, { role: "assistant", text: "", streaming: true }]);
    const controller = new AbortController();
    abortRef.current = controller;
    const append = (text: string) =>
      setMessages((m) => {
        const copy = [...m];
        const last = copy[copy.length - 1];
        if (last?.role === "assistant" && last.streaming) copy[copy.length - 1] = { ...last, text: last.text + text };
        return copy;
      });
    try {
      const res = await openAssistantTurn(conversationId, request, workspaceId, controller.signal);
      let proposal: AssistantProposal | null = null;
      let completed = false;
      let turnError: ApiError | null = null;
      for await (const evt of sseEvents(res)) {
        if (!alive.current) return null;
        const data = evt.data as Record<string, unknown>;
        if (evt.event === "delta") append(String(data.text ?? ""));
        else if (evt.event === "tool") {
          setMessages((m) => {
            const copy = [...m];
            const last = copy[copy.length - 1];
            if (last?.role === "assistant" && last.streaming) copy.pop();
            copy.push({ role: "tool", text: "", name: String(data.name ?? "") });
            copy.push(last?.role === "assistant" && last.streaming ? last : { role: "assistant", text: "", streaming: true });
            return copy;
          });
        } else if (evt.event === "tool_input") {
          // fills the arguments of the most recent tool card still waiting for them
          const toolInput = String(data.input ?? "");
          if (toolInput)
            setMessages((m) => {
              const copy = [...m];
              for (let k = copy.length - 1; k >= 0; k--) {
                if (copy[k].role === "tool" && !copy[k].text) {
                  copy[k] = { ...copy[k], text: toolInput };
                  break;
                }
              }
              return copy;
            });
        } else if (evt.event === "proposal") {
          proposal = data as unknown as AssistantProposal;
          if (proposal.status === "invalid") {
            // mirrors the transcript row the backend stored for this rejection
            const errs = proposal.validation_errors.map((e) => `- ${e}`).join("\n");
            setMessages((m) => [...m, { role: "error", name: "proposal_rejected", text: errs }]);
          }
        } else if (evt.event === "error") {
          const code = typeof data.code === "string" ? data.code : null;
          turnError = new ApiError(code ?? "assistant.turn_failed", String(data.message ?? t("assistantEval.repairFailed")), null);
          setMessages((m) => [
            ...m.filter((x) => !(x.role === "assistant" && x.streaming && !x.text)),
            {
              role: "error",
              text: code ? tRef.current(`apiErrors.${code}`, String(data.message ?? "")) : String(data.message ?? ""),
            },
          ]);
        } else if (evt.event === "done") completed = true;
      }
      if (!alive.current) return null;
      setMessages((m) => m.map((x) => ({ ...x, streaming: false })));
      // re-read the server's transcript + proposals: the ledger is the truth
      const detail = await reload(conversationId);
      if (!detail || !alive.current) return null;
      if (repairing) {
        if (turnError) throw turnError;
        if (!completed) throw new Error(t("assistantEval.repairIncomplete"));
        const saved = detail.proposals.find((p) =>
          p.id === proposal?.id && p.revision === proposal.revision && p.content_hash === proposal.content_hash);
        if (
          !saved || saved.conversation_id !== conversationId || saved.source !== "model"
          || saved.revision <= (latest?.revision ?? 0) || saved.status !== "draft" || saved.validation_errors.length > 0
        ) throw new Error(t("assistantEval.repairNoProposal"));
        return saved;
      }
      if (proposal) setEditing(null);
      return null;
    } catch (err) {
      if (!alive.current || controller.signal.aborted) return null;
      setMessages((m) => [
        ...m.filter((x) => !(x.role === "assistant" && x.streaming && !x.text)),
        { role: "error", text: t("assistantPage.turnFailed", { msg: apiMessage(err) }) },
      ]);
      if (isAssistantUnauthorized(err)) toast("error", t("assistantPage.expiredSession"));
      else if (err instanceof ApiError && err.code === "assistant.unavailable") onUnavailable();
      if (repairing) throw err;
      return null;
    } finally {
      if (alive.current) setBusy(false);
      if (abortRef.current === controller) abortRef.current = null;
    }
  };

  const refreshCatalog = async () => {
    if (!conversation) return;
    const conversationId = conversation.id;
    try {
      const res = await api.assistantRefreshCatalog(conversationId, workspaceId);
      if (!alive.current) return;
      setConversation((c) => (c && c.id === conversationId ? res.conversation ?? { ...c, catalog: res.catalog } : c));
      toast("success", t("assistantPage.catalogChanged"));
    } catch (err) {
      if (alive.current) toast("error", apiMessage(err));
    }
  };

  const approve = async (pin: PinnedApproval) => {
    if (resourcesDirty || resourcesLocked) return;
    setConfirm(null);
    if (conversation?.id !== pin.conversationId) return;
    setApproving(true);
    try {
      const res = await api.assistantApproveProposal(pin.conversationId, pin.revision, pin.hash);
      if (!alive.current) return;
      toast("success", res.started
        ? t("assistantPage.approvedToast", { job: shortId(res.job_id) })
        : t("assistantPage.alreadyApprovedToast"));
      await reload(pin.conversationId);
    } catch (err) {
      if (!alive.current) return;
      toast("error", apiMessage(err));
      if (isAssistantUnauthorized(err)) onUnavailable();
      else await reload(pin.conversationId).catch(() => null);
    } finally {
      if (alive.current) setApproving(false);
    }
  };

  const reject = async (revision: number) => {
    if (!conversation) return;
    const conversationId = conversation.id;
    setConfirm(null);
    try {
      await api.assistantRejectProposal(conversationId, revision);
      if (!alive.current) return;
      toast("success", t("assistantPage.rejectedToast"));
      setEditing(null);
      await reload(conversationId);
    } catch (err) {
      if (!alive.current) return;
      toast("error", apiMessage(err));
      await reload(conversationId).catch(() => null);
    }
  };

  const saveEdit = async () => {
    if (!conversation || !latest || !editing) return;
    if (Object.values(proposalEditErrors(editing)).some(Boolean)) {
      toast("error", t("assistantPage.editInvalid"));
      return;
    }
    const conversationId = conversation.id;
    try {
      const res = await api.assistantEditProposal(conversationId, proposalContentFromDraft(editing, latest.content));
      if (!alive.current) return;
      toast("success", t("assistantPage.editSavedToast", { n: res.proposal.revision }));
      setEditing(null);
      await reload(conversationId);
    } catch (err) {
      if (alive.current) toast("error", apiMessage(err));
    }
  };

  const canDeploy = (status.can_deploy ?? can("agents.deploy")) && status.deploy_requirements.length === 0;
  const deployReason = !status.can_deploy
    ? t("assistantPage.noDeployPermission")
    : status.deploy_requirements.length
      ? t("assistantPage.deployBlocked", { list: status.deploy_requirements.map((r) => r.message).join("; ") })
      : undefined;

  if (!conversation) {
    return (
      <>
        <FlowHeader title={t("nav.assistant")} onBack={back} />
        {loadError ? (
          <Alert tone="error" action={<button type="button" className="v2-link" onClick={() => setNonce((n) => n + 1)}>{t("v2.common.retry")}</button>}>
            {loadError}
          </Alert>
        ) : (
          <Spin />
        )}
      </>
    );
  }

  const title = conversation.title || conversation.id.slice(0, 8);
  const approveDisabled = !canDeploy || approving || busy || preparing || resourcesDirty;
  const approveReason = resourcesDirty ? t("assistantProgress.hints.unsaved") : deployReason;

  return (
    <div data-testid="v2-assistant-detail" data-conversation={conversation.id}>
      <FlowHeader
        title={
          <span className="v2-row">
            {title}
            {latest && (
              <>
                <Tag tone={PROPOSAL_TONE[latest.status]} dot>{t(`assistantPage.status.${latest.status}`)}</Tag>
                <Tag tone="outline">{t("assistantPage.revision", { n: latest.revision })}</Tag>
              </>
            )}
            {conversation.turn_in_progress !== null && <Tag tone="blue">{t("assistantPage.streaming")}</Tag>}
          </span>
        }
        onBack={back}
        end={
          <>
            <Button kind="danger" disabled={busy || clear.busy} title={t("assistantPage.clear.action")}
              onClick={() => void clear.ask(conversation)} testId="v2-assistant-clear">
              {t("v2.assistant.clear")}
            </Button>
            <Button kind="primary" disabled={busy || creating} onClick={() => void newConversation()} testId="v2-assistant-new">
              <Plus size={14} aria-hidden="true" />
              {t("assistantPage.newConversation")}
            </Button>
          </>
        }
      />
      <CreationProgressCard latest={latest} deployed={deployed} resourcesDirty={resourcesDirty} editing={editing !== null} />

      <div className="v2-assistant-grid">
        <DiscussionCard
          conversation={conversation}
          messages={messages}
          input={input}
          onInput={setInput}
          busy={busy}
          preparing={preparing}
          onSend={() => void send({ prompt: input })}
          onRefreshCatalog={() => void refreshCatalog()}
          threadRef={threadRef}
        />

        <section id={SECTION_IDS.proposal} className="v2-card" data-testid="v2-assistant-proposal">
          <div className="v2-card-body">
            <h2 className="v2-sec-title">
              {t("assistantPage.proposalTitle")}
              <span className="sub">{t("assistantPage.proposalSub")}</span>
              {latest && (
                <span className="end">
                  <Tag tone="outline">{t("assistantPage.revision", { n: latest.revision })}</Tag>
                  <Tag tone={PROPOSAL_TONE[latest.status]} dot>{t(`assistantPage.status.${latest.status}`)}</Tag>
                </span>
              )}
            </h2>
            {!latest && <div className="v2-muted" data-testid="v2-assistant-proposal-empty">{t("assistantPage.proposalEmpty")}</div>}
            {latest && !editing && (
              <ProposalView proposal={latest} catalog={conversation.catalog} account={status.account_id} region={status.region} />
            )}
            {latest && editing && (
              <ProposalEditor
                draft={editing}
                catalog={conversation.catalog}
                capabilities={status.capabilities}
                errors={proposalEditErrors(editing)}
                onChange={setEditing}
                resourcesLocked={resourcesLocked}
              />
            )}
            {latest && (
              <>
                <div style={{ marginTop: 16 }}>
                  <Alert>{t(resourcesLocked ? "assistantPreparation.reviewOnly" : "assistantPage.supportedHere")}</Alert>
                  {latest.status === "draft" && !resourcesLocked && (
                    <Alert tone="warn">{t("assistantPage.billable", { account: status.account_id, region: status.region })}</Alert>
                  )}
                </div>
                <div className="v2-assistant-actions">
                  {editing ? (
                    <>
                      <Button onClick={() => setEditing(null)}>{t("assistantPage.cancelEdit")}</Button>
                      <Button kind="primary" onClick={() => void saveEdit()} testId="v2-assistant-edit-save">
                        {t("assistantPage.saveEdit")}
                      </Button>
                    </>
                  ) : (
                    <>
                      {(latest.status === "draft" || latest.status === "invalid") && (
                        <>
                          <Button disabled={busy || preparing} testId="v2-assistant-proposal-edit"
                            onClick={() => setEditing(proposalEditStart(latest, conversation, resourcesLocked))}>
                            {t("assistantPage.edit")}
                          </Button>
                          <Button disabled={busy || preparing} testId="v2-assistant-proposal-reject"
                            onClick={() => setConfirm({ kind: "reject", revision: latest.revision })}>
                            {t("assistantPage.reject")}
                          </Button>
                        </>
                      )}
                      <span className="spacer" />
                      {latest.status === "draft" && !resourcesLocked && (
                        <>
                          {approveDisabled && approveReason && (
                            <span className="v2-muted" style={{ fontSize: 12.5 }}>{approveReason}</span>
                          )}
                          <Button
                            kind="primary"
                            disabled={approveDisabled}
                            title={approveReason}
                            testId="v2-assistant-proposal-approve"
                            onClick={() => setConfirm({
                              kind: "approve",
                              pin: {
                                conversationId: conversation.id,
                                revision: latest.revision,
                                hash: latest.content_hash,
                                name: String(latest.content.name ?? ""),
                              },
                            })}
                          >
                            {approving ? t("assistantPage.approving") : t("assistantPage.approve")}
                          </Button>
                        </>
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
              />
            )}
            {olderApprovals.length > 0 && (
              <div className="v2-assistant-section" data-testid="v2-assistant-outcome-history">
                <h3>{t("assistantPage.outcomeHistory")}</h3>
                <ul className="mono" style={{ fontSize: 12 }}>
                  {olderApprovals.map((p) => (
                    <li key={p.id}>
                      {t("assistantPage.revision", { n: p.revision })} · {p.approval?.agent_name} ·{" "}
                      {String(p.approval?.agent_status ?? "").toUpperCase()} · job {shortId(p.approval?.job_id)} ·{" "}
                      {String(p.approval?.job_status ?? "").toUpperCase()}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </section>
      </div>

      <PreparationCard
        key={`prep:${workspaceId}:${conversation.id}`}
        conversation={conversation}
        workspaceId={workspaceId}
        disabled={busy || approving || editing !== null || conversation.turn_in_progress !== null}
        onWorking={setPreparing}
        locked={resourcesLocked}
        onDirty={setResourcesDirty}
        onUpdated={(detail) => {
          if (!alive.current) return;
          setConversation((c) => (c?.id === detail.id ? detail : c));
          setEditing(null);
          setConfirm(null);
        }}
        onCatalog={(catalog) => {
          if (!alive.current) return;
          setConversation((c) => (c?.id === conversation.id ? { ...c, catalog } : c));
        }}
        onDiscuss={(text) => {
          setInput(text);
          const el = document.getElementById(COMPOSER_ID);
          el?.scrollIntoView({ behavior: "smooth", block: "center" });
          el?.focus();
        }}
      />

      {latest ? (
        <EvalAssetsCard
          key={`eval:${workspaceId}:${conversation.id}`}
          conversationId={conversation.id}
          proposals={conversation.proposals}
          canMaterialize={status.can_materialize_evaluation_assets}
          workspaceId={workspaceId}
          apiMessage={apiMessage}
          onError={(m) => toast("error", m)}
          deployed={deployed}
          repairDisabledReason={repairDisabledReason}
          onRepair={(repair) => send({ prompt: t("assistantEval.repairPrompt"), evaluation_plan_repair: repair })}
        />
      ) : (
        <Card title={t("assistantEval.title")} sub={t("assistantEval.sub")}>
          <div id={SECTION_IDS.evaluation} className="v2-muted">{t("v2.assistant.evalAfterProposal")}</div>
        </Card>
      )}

      <AdlcCard open={false} />

      <Confirm
        open={confirm?.kind === "approve"}
        title={t("assistantPage.approveTitle", { n: confirm?.kind === "approve" ? confirm.pin.revision : 0 })}
        body={<Alert tone="warn">{t("assistantPage.approveBody", {
          name: confirm?.kind === "approve" ? confirm.pin.name : "",
          account: status.account_id,
          region: status.region,
        })}</Alert>}
        confirmLabel={t("assistantPage.approveConfirm")}
        onConfirm={() => { if (confirm?.kind === "approve") void approve(confirm.pin); }}
        onClose={() => setConfirm(null)}
      />
      <Confirm
        open={confirm?.kind === "reject"}
        danger
        title={t("assistantPage.rejectTitle")}
        body={t("assistantPage.rejectBody", { n: confirm?.kind === "reject" ? confirm.revision : 0 })}
        confirmLabel={t("assistantPage.rejectConfirm")}
        onConfirm={() => { if (confirm?.kind === "reject") void reject(confirm.revision); }}
        onClose={() => setConfirm(null)}
      />
      {clear.dialog}
    </div>
  );
}

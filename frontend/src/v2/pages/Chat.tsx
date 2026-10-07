import { Plus, RefreshCw, Square } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { AttachmentHint } from "../../components/chat/AttachmentViews";
import type { PendingAttachment } from "../../components/chat/attachments";
import {
  attachmentMediaType,
  attachmentMetadata,
  encodeAttachment,
  validateAttachments,
} from "../../components/chat/attachments";
import { methodLabel } from "../../components/methodChipMeta";
import {
  api,
  chatApi,
  type ChatMemorySummary,
  type ChatRequest,
  type ChatSessionInfo,
  type ChatTraceInfo,
  type AuthRequiredEvent,
  errorMessage,
  localizedMessage,
} from "../../lib/api";
import {
  agentMemoryState,
  type AsUserChoice,
  asUserChecked,
  asUserField,
  chatEligible,
  isHarnessAgent,
  sseEvents,
} from "../../lib/chat";
import { livePolicyDeny, restoredPolicyDeny } from "../../lib/policy-deny";
import { liveAuthAsk, restoredAuthAsk } from "../../lib/user-grants";
import { useLoad, useV2Toast } from "../hooks";
import { Alert, Button, Card, Confirm, LinkButton, PageHeader, Select, Spin, Tag } from "../ui";
import "./chat/chat.css";
import { Composer } from "./chat/Composer";
import { Inspector, type InspectorTab } from "./chat/Inspector";
import { SessionRail } from "./chat/SessionRail";
import { appendDelta } from "./chat/messages";
import { type ChatMessage, Thread } from "./chat/Thread";

const MEMORY_TAG = {
  on: "v2.chat.memoryOn",
  off: "v2.chat.memoryOff",
  "agent-defined": "v2.chat.memoryAgentDefined",
} as const;

/**
 * 对话调试 — live SSE chat with any active agent, over the same invoke chain as
 * the public `/v1` API. Deep links: `?agent=<id>` preselects the agent,
 * `&session=<id>` replays and resumes that conversation.
 */
export function V2Chat() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { authRequired, username } = useAuth();
  const [params, setParams] = useSearchParams();
  const linkedAgent = params.get("agent");
  const linkedSession = params.get("session");

  const agentsLoad = useLoad(() => api.listAgents(), "chat-agents");
  const agents = useMemo(
    () => (agentsLoad.data ? chatEligible(agentsLoad.data.agents) : []),
    [agentsLoad.data],
  );
  const [agentId, setAgentId] = useState("");
  // "?agent=<id>" that no active agent matches: the notice names it and the
  // picker stays unselected — never silently another agent's runtime.
  const [staleAgent, setStaleAgent] = useState<string | null>(null);
  const resolvedRef = useRef(false);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  // JWT-inbound agents: null = auto (as_user omitted: the member's pool JWT when
  // signed in, else the workspace M2M token); a toggle pins it explicitly.
  const [asUser, setAsUser] = useState<AsUserChoice>(null);
  /** who the Runtime authenticated on the last JWT turn (meta.inbound.caller) */
  const [caller, setCaller] = useState<"user_jwt" | "m2m" | null>(null);
  const [input, setInput] = useState("");
  const [draft, setDraft] = useState<{ files: PendingAttachment[]; error: string | null }>({
    files: [],
    error: null,
  });
  const pendingFiles = draft.files;
  const setAttachmentError = (error: string | null) => setDraft((d) => ({ ...d, error }));
  const resetAttachments = () => setDraft({ files: [], error: null });
  const [sessionId, setSessionId] = useState<string | null>(linkedSession);
  const [busy, setBusy] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const sendInFlight = useRef(false);
  const historyRequest = useRef(0);
  const restoredRef = useRef(false);
  const [sessions, setSessions] = useState<ChatSessionInfo[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  // session id whose END request is in flight (one at a time)
  const [ending, setEnding] = useState<string | null>(null);
  const [confirmEnd, setConfirmEnd] = useState<string | null>(null);
  const [memory, setMemory] = useState<ChatMemorySummary | null>(null);
  const [trace, setTrace] = useState<ChatTraceInfo | null>(null);
  const [traceBusy, setTraceBusy] = useState(false);
  const [tab, setTab] = useState<InspectorTab>("trace");

  // Resolve the deep link once, when the agent list first answers.
  useEffect(() => {
    if (!agentsLoad.data || resolvedRef.current) return;
    resolvedRef.current = true;
    const linked = linkedAgent ? agents.find((a) => a.id === linkedAgent) : undefined;
    if (linked) {
      setAgentId(linked.id);
    } else if (linkedAgent) {
      // Unknown/inactive: say so, drop the linked session too (a foreign session
      // id is never posted to a different agent's runtime), strip both params.
      setStaleAgent(linkedAgent);
      if (linkedSession) setSessionId(null);
      setParams({}, { replace: true });
    } else if (agents.length) {
      setAgentId(agents[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentsLoad.data]);

  const loadSessions = async (aid: string) => {
    setSessionsLoading(true);
    try {
      setSessions((await api.listChatSessions(aid)).sessions);
    } catch {
      /* history rail is best-effort */
    } finally {
      setSessionsLoading(false);
    }
  };

  useEffect(() => {
    if (agentId) void loadSessions(agentId);
    else setSessions([]);
  }, [agentId]);

  const refreshMemory = async (sid: string) => {
    if (!agentId) return;
    try {
      setMemory(await chatApi.memory(agentId, sid));
    } catch {
      /* memory rail is best-effort */
    }
  };

  useEffect(() => {
    // agentId in deps: on a deep-linked session the agent resolves after mount.
    if (sessionId && agentId && !busy) void refreshMemory(sessionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, busy, agentId]);

  const restoreSession = async (sid: string) => {
    if (!agentId || busy) return;
    const requestId = ++historyRequest.current;
    setRestoring(true);
    try {
      const rows = (await chatApi.history(agentId, sid)).messages;
      if (requestId !== historyRequest.current) return;
      setMessages(
        rows.map((r): ChatMessage =>
          r.role === "user"
            ? { kind: "user", text: r.text, attachments: r.attachments }
            : r.role === "agent"
              ? { kind: "agent", text: r.text }
              : r.role === "tool"
                ? { kind: "tool", text: r.name ?? "tool", name: r.name ?? "tool" }
                : r.role === "auth"
                  ? // the URL is single-use and never persisted: a restored card retries
                    {
                      kind: "auth",
                      text: r.text,
                      name: r.name ?? "",
                      auth: restoredAuthAsk(r, agentId),
                    }
                  : r.role === "policy"
                    ? { kind: "policy", text: r.text, name: r.name ?? "", policy: restoredPolicyDeny(r) }
                    : { kind: "error", text: r.text },
        ),
      );
      setSessionId(sid);
      setInput("");
      // A successful history selection discards the previous conversation's draft.
      resetAttachments();
      setTrace(null);
      setParams({ agent: agentId, session: sid }, { replace: true });
    } catch (err) {
      if (requestId === historyRequest.current) toast("error", errorMessage(err));
    } finally {
      if (requestId === historyRequest.current) setRestoring(false);
    }
  };

  // Reload / deep link with a session in the URL: back-fill the thread once the
  // agent is resolved, so the conversation is visible, not just resumable.
  useEffect(() => {
    if (!restoredRef.current && agentId && sessionId && messages.length === 0) {
      restoredRef.current = true;
      void restoreSession(sessionId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, sessionId]);

  const agent = agents.find((a) => a.id === agentId);
  const memoryState = agentMemoryState(agent);
  const jwtAgent = agent?.inbound_auth_mode === "jwt";
  const capability = agent?.attachment_capability;
  const attachmentsEnabled = Boolean(
    capability && (capability.images || capability.text || capability.pdf !== "unsupported"),
  );

  // `retryPrompt`: an auth card's retry re-sends that turn's text, never the
  // composer's draft or its pending files.
  const send = async (retryPrompt?: string) => {
    const retrying = retryPrompt !== undefined;
    const prompt = (retrying ? retryPrompt : input).trim();
    const files = retrying ? [] : pendingFiles;
    if ((!prompt && !files.length) || !agentId || busy || restoring || sendInFlight.current) return;
    if (files.length) {
      const invalid = validateAttachments(files.map((f) => f.file), capability);
      if (invalid) {
        setAttachmentError(t(`chatPage.attachments.${invalid.key}`, { ...invalid }));
        return;
      }
    }
    sendInFlight.current = true;
    if (!retrying) {
      setInput("");
      setAttachmentError(null);
    }
    setBusy(true);
    const userMessage: ChatMessage = {
      kind: "user",
      text: prompt,
      attachments: capability && files.length ? files.map((f) => attachmentMetadata(f, capability)) : undefined,
    };
    setMessages((m) => [...m, userMessage]);
    let failed = false;
    let completed = false;
    let activeSessionId = sessionId;
    try {
      const request: ChatRequest = { prompt, session_id: sessionId };
      const asUserValue = asUserField(jwtAgent, asUser);
      if (asUserValue !== undefined) request.as_user = asUserValue;
      if (files.length) {
        request.attachments = await Promise.all(files.map(encodeAttachment)).catch(() => {
          throw new Error(t("chatPage.attachments.readFailed"));
        });
      }
      const res = await chatApi.stream(agentId, request);
      // Encoded bytes are request-only; release them while the response streams.
      delete request.attachments;
      if (!res.body) throw new Error(t("chatPage.streamInterrupted"));
      let agentOpen = false;
      for await (const { event, data: payload } of sseEvents(res)) {
        if (event === "meta") {
          if (payload.session_id) {
            activeSessionId = payload.session_id;
            setSessionId(payload.session_id);
            // keep the session in the URL so a reload restores this conversation
            setParams({ agent: agentId, session: payload.session_id }, { replace: true });
          }
          setCaller(payload.inbound?.caller ?? null);
          if (payload.attachments) {
            setMessages((m) =>
              m.map((msg) => (msg === userMessage ? { ...msg, attachments: payload.attachments } : msg)),
            );
          }
        } else if (event === "tool") {
          setMessages((m) => [...m, { kind: "tool", text: payload.name ?? "tool", name: payload.name }]);
          agentOpen = false;
        } else if (event === "auth_required") {
          const ask: Partial<AuthRequiredEvent> = payload;
          setMessages((m) => [
            ...m,
            {
              kind: "auth",
              text: ask.provider ?? "",
              name: ask.tool,
              auth: liveAuthAsk(ask, agentId),
            },
          ]);
          // the answer keeps streaming into the same bubble around the card
        } else if (event === "policy_denied") {
          // follows its tool row, which already closed the open bubble
          const policy = livePolicyDeny(payload);
          setMessages((m) => [...m, { kind: "policy", text: policy.reason, name: policy.tool, policy }]);
        } else if (event === "delta") {
          const open = agentOpen;
          setMessages((m) => appendDelta(m, payload.text ?? "", open));
          agentOpen = true;
        } else if (event === "error") {
          failed = true;
          const message = localizedMessage(payload.code ?? "", payload.message ?? t("chatPage.sendFailed"));
          setMessages((m) => [...m, { kind: "error", text: message }]);
          if (files.length) setAttachmentError(message);
        } else if (event === "done") {
          completed = true;
          // Only an agent whose spec turns memory on persists the turn; the
          // console itself never writes memory.
          if (!failed && memoryState === "on") setMessages((m) => [...m, { kind: "memory", text: "" }]);
        }
      }
      if (!completed && !failed) throw new Error(t("chatPage.streamInterrupted"));
      if (completed && !failed && !retrying) resetAttachments();
    } catch (err) {
      failed = true;
      const message = errorMessage(err);
      setMessages((m) => [...m, { kind: "error", text: message }]);
      if (files.length) setAttachmentError(message);
    } finally {
      if (failed && !retrying) setInput(prompt);
      setMessages((m) => m.map((msg) => (msg.streaming ? { ...msg, streaming: false } : msg)));
      sendInFlight.current = false;
      setBusy(false);
      if (activeSessionId) void refreshMemory(activeSessionId);
      if (agentId) void loadSessions(agentId);
    }
  };

  const newSession = (aid: string = agentId) => {
    if (sendInFlight.current) return;
    historyRequest.current += 1;
    setRestoring(false);
    setInput("");
    resetAttachments();
    setSessionId(null);
    setMessages([]);
    setCaller(null);
    setMemory(null);
    setTrace(null);
    setParams(aid ? { agent: aid } : {}, { replace: true });
  };

  // END: terminate the live AgentCore Runtime session (not just forget the id,
  // which is all 新会话 does). The ledger row stays replayable.
  const endSession = async (sid: string) => {
    if (!agentId || ending) return;
    setEnding(sid);
    try {
      const result = await api.stopChatSession(agentId, sid);
      toast("success", t(result.already_ended ? "chatPage.endedAlready" : "chatPage.ended"));
      // the ended id must not receive the next prompt — same reset as 新会话
      if (sid === sessionId) newSession();
      void loadSessions(agentId);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setEnding(null);
    }
  };

  const loadTrace = async () => {
    if (!sessionId) return;
    setTraceBusy(true);
    try {
      setTrace(await chatApi.trace(sessionId));
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setTraceBusy(false);
    }
  };

  const addFiles = (files: File[]) => {
    if (composerDisabled || !files.length) return;
    const additions = files.map((file) => ({ id: crypto.randomUUID(), file, mediaType: attachmentMediaType(file) }));
    // Validate and append atomically so batched drop/paste events see prior additions.
    setDraft((d) => {
      const invalid = validateAttachments([...d.files.map((f) => f.file), ...files], capability);
      return invalid
        ? { ...d, error: t(`chatPage.attachments.${invalid.key}`, { ...invalid }) }
        : { files: [...d.files, ...additions], error: null };
    });
  };

  const composerDisabled = busy || restoring || !agentId;
  const sendDisabledReason =
    busy || restoring
      ? undefined
      : !agentId
        ? t("chatPage.sendDisabledNoAgent")
        : !input.trim() && !pendingFiles.length
          ? t(attachmentsEnabled ? "chatPage.sendDisabledEmpty" : "chatPage.sendDisabledEmptyText")
          : undefined;
  const harness = isHarnessAgent(agent);
  const currentEnded = Boolean(sessionId && sessions.find((s) => s.session_id === sessionId)?.ended_at);
  const endReason = (sid: string | null, ended: boolean): string | undefined =>
    !sid
      ? t("chatPage.endDisabledNoSession")
      : harness
        ? t("chatPage.endDisabledHarness")
        : ended
          ? t("chatPage.endDisabledEnded")
          : ending
            ? t("chatPage.endDisabledBusy")
            : busy
              ? t("chatPage.waitForReply")
              : undefined;
  const currentEndReason = endReason(sessionId, currentEnded);
  const userLabel = authRequired && username ? username : t("v2.chat.you");

  return (
    <>
      <PageHeader title={t("nav.chat")} desc={t("v2.chat.desc")} />
      {staleAgent !== null && (
        <Alert
          tone="warn"
          action={<LinkButton onClick={() => setStaleAgent(null)}>{t("staleLink.dismiss")}</LinkButton>}
        >
          {t("staleLink.bodyPicker", { kind: t("staleLink.kind.agent"), id: staleAgent })}
        </Alert>
      )}
      <div className="v2-chat-grid">
        <div className="v2-chat-col">
          <div className="v2-chat-pane">
            <Card
              title={t("v2.chat.agent")}
              end={
                <LinkButton onClick={agentsLoad.reload} title={t("v2.common.refresh")} disabled={busy}>
                  <RefreshCw size={13} aria-hidden="true" />
                </LinkButton>
              }
            >
              {agentsLoad.error && agents.length === 0 ? (
                <Alert
                  tone="error"
                  action={<LinkButton onClick={agentsLoad.reload}>{t("v2.common.retry")}</LinkButton>}
                >
                  {agentsLoad.error}
                </Alert>
              ) : (
                <Select
                  value={agentId}
                  disabled={busy || (agentsLoad.loading && !agentsLoad.data)}
                  placeholder={
                    agents.length === 0
                      ? agentsLoad.loading && !agentsLoad.data
                        ? t("v2.common.loading")
                        : t("v2.chat.noAgents")
                      : undefined
                  }
                  options={[
                    ...(agents.length > 0 && agentId === ""
                      ? [{ value: "", label: t("chatPage.pickAgent"), disabled: true }]
                      : []),
                    ...agents.map((a) => ({ value: a.id, label: a.name })),
                  ]}
                  onChange={(v) => {
                    setAgentId(v);
                    newSession(v);
                  }}
                  ariaLabel={t("chatPage.agentSelectLabel")}
                  testId="agent-select"
                />
              )}
              {agent && (
                <div className="v2-chat-agent-meta">
                  <Tag tone="blue">{methodLabel(agent.method)}</Tag>
                  <span data-testid="chat-memory-state" data-state={memoryState}>
                    <Tag tone={memoryState === "on" ? "green" : "gray"} dot={memoryState === "on"}>
                      {t(MEMORY_TAG[memoryState])}
                    </Tag>
                  </span>
                  {jwtAgent && <Tag tone="orange">{t("inboundAuth.mode.jwt")}</Tag>}
                  <Link className="v2-chat-extlink" to={`/v2/agents?view=detail&id=${encodeURIComponent(agent.id)}`}>
                    {t("v2.chat.agentDetail")}
                  </Link>
                </div>
              )}
              {jwtAgent && (
                <label className="v2-check v2-chat-asuser" title={t("v2.chat.asUserHint")} data-testid="chat-as-user">
                  <input
                    type="checkbox"
                    checked={asUserChecked(asUser, authRequired && !!username)}
                    disabled={busy}
                    onChange={(e) => setAsUser(e.target.checked)}
                  />
                  {t("v2.chat.asUser")}
                  {caller && (
                    <Tag tone={caller === "user_jwt" ? "green" : "gray"} testId="chat-caller">
                      {t(`v2.chat.caller.${caller}`)}
                    </Tag>
                  )}
                </label>
              )}
              {!agentsLoad.error && agentsLoad.data && agents.length === 0 && (
                <div className="v2-chat-rail-empty">
                  <Link to="/v2/agents?view=new">{t("v2.chat.deployFirst")}</Link>
                </div>
              )}
            </Card>
          </div>
          <div className="v2-chat-pane v2-chat-pane-grow">
            <Card
              title={t("v2.chat.history")}
              sub={sessions.length ? String(sessions.length) : undefined}
              end={
                <Button size="sm" disabled={busy || !agentId} onClick={() => newSession()} testId="new-session">
                  <Plus size={13} aria-hidden="true" />
                  {t("v2.chat.newSession")}
                </Button>
              }
            >
              {!agentId ? (
                <div className="v2-chat-rail-empty">{t("chatPage.sendDisabledNoAgent")}</div>
              ) : sessionsLoading && sessions.length === 0 ? (
                <Spin />
              ) : (
                <SessionRail
                  sessions={sessions}
                  currentId={sessionId}
                  busy={busy}
                  ending={ending}
                  endReason={endReason}
                  onOpen={(sid) => void restoreSession(sid)}
                  onEnd={(s) => setConfirmEnd(s.session_id)}
                />
              )}
            </Card>
          </div>
        </div>

        <div className="v2-chat-pane v2-chat-pane-main">
          <Card
            title={
              <span className="v2-row">
                {agent?.name ?? t("v2.chat.conversation")}
                {sessionId && (
                  <Tag tone="outline" title={sessionId}>
                    <span className="mono">session {sessionId.slice(0, 8)}…</span>
                  </Tag>
                )}
                {currentEnded && <Tag tone="gray">{t("v2.chat.ended")}</Tag>}
              </span>
            }
            end={
              <span className="v2-row">
                {sessionId && (
                  <LinkButton
                    onClick={() => {
                      setTab("trace");
                      void loadTrace();
                    }}
                    disabled={traceBusy}
                    testId="view-trace"
                  >
                    {t("v2.chat.viewTrace")}
                  </LinkButton>
                )}
                <Button
                  size="sm"
                  disabled={currentEndReason !== undefined}
                  title={currentEndReason}
                  onClick={() => sessionId && setConfirmEnd(sessionId)}
                  testId="end-session"
                >
                  <Square size={12} aria-hidden="true" />
                  {ending && ending === sessionId ? "…" : t("v2.chat.endSession")}
                </Button>
              </span>
            }
          >
            {agent && (
              <div className="v2-chat-caps">
                <AttachmentHint capability={capability} />
              </div>
            )}
            <Thread
              messages={messages}
              userLabel={userLabel}
              agentLabel={agent?.name ?? "Agent"}
              restoring={restoring}
              onRetry={(prompt) => void send(prompt)}
              retryDisabled={busy || restoring}
            />
            <Composer
              value={input}
              onChange={setInput}
              onSend={() => void send()}
              onAddFiles={addFiles}
              onRemoveFile={(id) => setDraft((d) => ({ files: d.files.filter((f) => f.id !== id), error: null }))}
              files={pendingFiles}
              attachmentError={draft.error}
              capability={capability}
              attachmentsEnabled={attachmentsEnabled}
              disabled={composerDisabled}
              sendDisabledReason={sendDisabledReason}
              placeholder={agent ? t("chatPage.placeholder", { name: agent.name }) : t("chatPage.pickAgent")}
            />
          </Card>
        </div>

        <div className="v2-chat-pane v2-chat-pane-side">
          <Card>
            <Inspector
              tab={tab}
              onTab={setTab}
              agentId={agentId}
              sessionId={sessionId}
              trace={trace}
              traceBusy={traceBusy}
              onLoadTrace={() => void loadTrace()}
              memory={memory}
              memoryState={memoryState}
            />
          </Card>
        </div>
      </div>
      <Confirm
        open={confirmEnd !== null}
        title={t("v2.chat.confirmEndTitle")}
        body={t("v2.chat.confirmEndBody")}
        confirmLabel={t("v2.chat.endSession")}
        danger
        busy={ending !== null}
        onConfirm={() => {
          if (confirmEnd) void endSession(confirmEnd);
          setConfirmEnd(null);
        }}
        onClose={() => setConfirmEnd(null)}
      />
    </>
  );
}

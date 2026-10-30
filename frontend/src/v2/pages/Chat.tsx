import { browserUuid } from "../../lib/browser-compat";
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
  ApiError,
  chatApi,
  type ChatMemorySummary,
  type ChatTraceInfo,
  type ChatRequest,
  type ChatSessionInfo,
  type ChatStreamPayload,
  type AuthRequiredEvent,
  errorMessage,
  localizedMessage,
} from "../../lib/api";
import { domWait } from "../../lib/assistant-turn";
import {
  agentMemoryState,
  chatEligible,
  isHarnessAgent,
  pollMemorySummary,
  type StaleAgentState,
  staleAgentState,
} from "../../lib/chat";
import { chatReattachPlan, giveUpChatMessages, markChatReconnecting, runChatTurnLink } from "../../lib/chat-turn";
import { livePolicyDeny, restoredPolicyDeny } from "../../lib/policy-deny";
import { liveAuthAsk, restoredAuthAsk } from "../../lib/user-grants";
import { useLoad, useV2Toast } from "../hooks";
import { Alert, Button, Card, Confirm, LinkButton, PageHeader, Select, Spin, Tag } from "../ui";
import "./chat/chat.css";
import { Composer } from "./chat/Composer";
import { Inspector, type InspectorTab } from "./chat/Inspector";
import { SessionRail } from "./chat/SessionRail";
import { appendDelta, markTruncated } from "./chat/messages";
import { applyToolDetail } from "./chat/messages";
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
// N16-obs76 (NC-E04-05): the deep-link notice's wording per `staleAgentState`
const STALE_AGENT_COPY: Record<StaleAgentState, string> = {
  deploying: "staleLink.bodyDeploying",
  inactive: "staleLink.bodyInactive",
  missing: "staleLink.bodyPicker",
};

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
  // picker stays unselected — never silently another agent's runtime. N16-obs76: the
  // wording follows the agent's state — still deploying / not invocable / gone.
  const [staleAgent, setStaleAgent] = useState<{ id: string; state: StaleAgentState; status: string } | null>(null);
  const resolvedRef = useRef(false);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
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
  // N-ARCH-obs15 v2: the subscription to the turn being streamed (the turn itself keeps
  // running server-side); an unmount aborts only this
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);
  const historyRequest = useRef(0);
  const restoredRef = useRef(false);
  const [sessions, setSessions] = useState<ChatSessionInfo[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  // session id whose END request is in flight (one at a time)
  const [ending, setEnding] = useState<string | null>(null);
  const [confirmEnd, setConfirmEnd] = useState<string | null>(null);
  const [memory, setMemory] = useState<ChatMemorySummary | null>(null);
  // Session the memory rail is showing; a late post-turn poll for another
  // session (the user switched or started a new one meanwhile) is dropped.
  const memorySessionRef = useRef<string | null>(null);
  const [tab, setTab] = useState<InspectorTab>("memory");
  // Inspector → TRACE: `GET /api/traces/{session_id}`, pull-based (spans land in
  // CloudWatch ~1 min after a turn). `traceFor` is the session the loaded trace
  // belongs to, so a stale answer for a previous session is never shown.
  const [trace, setTrace] = useState<ChatTraceInfo | null>(null);
  const [traceFor, setTraceFor] = useState<string | null>(null);
  const [traceBusy, setTraceBusy] = useState(false);
  const [traceError, setTraceError] = useState<string | null>(null);
  const traceRequest = useRef(0);

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
      const all = agentsLoad.data.agents;
      setStaleAgent({
        id: linkedAgent,
        state: staleAgentState(linkedAgent, all),
        status: all.find((a) => a.id === linkedAgent)?.status ?? "",
      });
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
      const summary = await chatApi.memory(agentId, sid);
      if (memorySessionRef.current === sid) setMemory(summary);
    } catch {
      /* memory rail is best-effort */
    }
  };

  // After a turn: the runtime writes the short-term event after it streamed
  // `done`, so a single fetch races it and shows the pre-turn count (N10-bug6).
  // Poll until the count grows past what the rail showed before the turn.
  const settleMemory = async (sid: string, previous: number) => {
    if (!agentId) return;
    const aid = agentId;
    await pollMemorySummary(() => chatApi.memory(aid, sid), previous, {
      onUpdate: (summary) => {
        if (memorySessionRef.current === sid) setMemory(summary);
      },
    });
  };

  const loadTrace = async (sid: string | null = sessionId) => {
    if (!sid) return;
    const requestId = ++traceRequest.current;
    setTraceBusy(true);
    setTraceError(null);
    try {
      const info = await chatApi.trace(sid);
      if (requestId !== traceRequest.current) return;
      setTrace(info);
      setTraceFor(sid);
    } catch (err) {
      if (requestId !== traceRequest.current) return;
      setTraceError(errorMessage(err));
    } finally {
      if (requestId === traceRequest.current) setTraceBusy(false);
    }
  };

  const resetTrace = () => {
    traceRequest.current += 1;
    setTrace(null);
    setTraceFor(null);
    setTraceError(null);
    setTraceBusy(false);
  };

  // The rail loads itself whenever the TRACE tab looks at a session it has no trace
  // for yet — opening the tab, restoring a session, or the first turn of a new session
  // completing (`busy` flips back). Later turns use LOAD TRACE (a Logs Insights scan
  // per load, so not on every turn).
  useEffect(() => {
    if (tab === "trace" && sessionId && !busy && !traceBusy && traceFor !== sessionId) void loadTrace(sessionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, sessionId, busy]);

  useEffect(() => {
    memorySessionRef.current = sessionId;
    // agentId in deps: on a deep-linked session the agent resolves after mount.
    // Not re-run on `busy`: the post-turn refresh is `settleMemory` in send().
    if (sessionId && agentId && !busy) void refreshMemory(sessionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, agentId]);

  const restoreSession = async (sid: string) => {
    if (!agentId || busy) return;
    const requestId = ++historyRequest.current;
    setRestoring(true);
    try {
      const history = await chatApi.history(agentId, sid);
      const rows = history.messages;
      if (requestId !== historyRequest.current) return;
      const restored =
        rows.map((r): ChatMessage =>
          r.role === "user"
            ? { kind: "user", text: r.text, attachments: r.attachments }
            : r.role === "agent"
              ? { kind: "agent", text: r.text, truncated: r.truncated === true }
              : r.role === "tool"
                ? {
                    kind: "tool",
                    text: r.name ?? "tool",
                    name: r.name ?? "tool",
                    // N16-obs85: additive — present only when the runtime streamed a summary
                    ...(r.tool_input !== undefined ? { toolInput: r.tool_input } : {}),
                    ...(r.tool_result_summary !== undefined ? { toolResult: r.tool_result_summary } : {}),
                    ...(r.tool_result_status ? { toolStatus: r.tool_result_status } : {}),
                  }
                : r.role === "auth"
                  ? // the URL is single-use and never persisted: a restored card retries
                    {
                      kind: "auth",
                      text: r.text,
                      name: r.name ?? "",
                      auth: restoredAuthAsk(r, agentId),
                    }
                  : r.role === "policy"
                    ? { kind: "policy", text: r.text, name: r.name ?? "", deny: restoredPolicyDeny(r) }
                    : { kind: "error", text: r.text },
        );
      setMessages(restored);
      setSessionId(sid);
      setInput("");
      // A successful history selection discards the previous conversation's draft
      // and the previous session's trace (the TRACE tab reloads for this one).
      resetAttachments();
      if (traceFor !== sid) resetTrace();
      setParams({ agent: agentId, session: sid }, { replace: true });
      // N-ARCH-obs15 v2: a turn still generating server-side is reattached to
      const plan = chatReattachPlan(restored, history.live_turn, authRequired ? username : "river");
      if (plan) void attachLive(sid, plan.turn, plan.messages);
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
  const capability = agent?.attachment_capability;
  const attachmentsEnabled = Boolean(
    capability && (capability.images || capability.text || capability.pdf !== "unsupported"),
  );

  /**
   * N-ARCH-obs15 v2: consume one turn's stream to its terminal event, reattaching across
   * connection losses — the turn keeps running server-side whatever happens here; an
   * unmount aborts only this subscription. `open` is the `POST /api/chat/{agent_id}`
   * response (a new turn) or the reattach itself (a page load with `live_turn`).
   */
  const consumeTurn = async (
    controller: AbortController,
    open: (signal: AbortSignal) => Promise<Response>,
    knownTurn: number | null,
    userMessage: ChatMessage | null,
    files: PendingAttachment[],
    initialSessionId: string | null,
  ) => {
    const aid = agentId;
    let failed = false;
    let completed = false;
    let activeSessionId = initialSessionId;
    let agentOpen = false;
    const outcome = await runChatTurnLink({
      agentId: aid,
      sessionId: () => activeSessionId,
      open,
      turn: knownTurn,
      signal: controller.signal,
      wait: domWait,
      onStatus: (status) => {
        setMessages((m) => markChatReconnecting(m, status === "reconnecting"));
        if (status === "reattached") toast("success", t("chatPage.turnReattached"));
      },
      onEvent: ({ event, data }) => {
        const payload = data as ChatStreamPayload;
        if (event === "meta") {
          if (payload.session_id) {
            activeSessionId = payload.session_id;
            memorySessionRef.current = payload.session_id;
            setSessionId(payload.session_id);
            // keep the session in the URL so a reload restores this conversation
            setParams({ agent: aid, session: payload.session_id }, { replace: true });
          }
          if (payload.attachments && userMessage) {
            setMessages((m) =>
              m.map((msg) => (msg === userMessage ? { ...msg, attachments: payload.attachments } : msg)),
            );
          }
        } else if (event === "tool") {
          setMessages((m) => [...m, { kind: "tool", text: payload.name ?? "tool", name: payload.name, toolId: payload.id ?? null }]);
          agentOpen = false;
        } else if (event === "tool_input" || event === "tool_result") {
          // N16-obs85: the bounded arguments / result summary complete the tool row the
          // call opened; neither closes the answer bubble
          setMessages((m) => applyToolDetail(m, payload));
        } else if (event === "auth_required") {
          const ask: Partial<AuthRequiredEvent> = payload;
          setMessages((m) => [
            ...m,
            {
              kind: "auth",
              text: ask.provider ?? "",
              name: ask.tool,
              auth: liveAuthAsk(ask, aid),
            },
          ]);
          // the answer keeps streaming into the same bubble around the card
        } else if (event === "delta") {
          const open = agentOpen;
          setMessages((m) => appendDelta(m, payload.text ?? "", open));
          agentOpen = true;
        } else if (event === "truncated" && payload.truncated) {
          // N-SCN-bug4: the open bubble could not finish at the model's token limit
          setMessages((m) => markTruncated(m));
        } else if (event === "policy_denied") {
          // the Gateway interceptor refused a tool call for this user (Global parity: policies
          // decide tool calls only — an invoke is never a policy decision); the answer keeps
          // streaming into the same bubble around the card
          setMessages((m) => [...m, { kind: "policy", text: payload.reason ?? "", name: payload.tool, deny: livePolicyDeny(payload) }]);
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
      },
    });
    if (outcome.kind === "gave_up") {
      // no terminal event within the server's claim TTL: the turn will not end on its own
      failed = true;
      setMessages((m) => giveUpChatMessages(m, t("chatPage.turnInterrupted")));
    }
    return { outcome, failed, completed, activeSessionId };
  };

  // A page load found a turn still generating: show the thread up to its user row,
  // replay the turn from seq 0 and tail it. Same busy ownership as `send`.
  const attachLive = async (sid: string, turn: number, initial: ChatMessage[]) => {
    if (!agentId || sendInFlight.current || abortRef.current) return;
    const aid = agentId;
    const controller = new AbortController();
    abortRef.current = controller;
    sendInFlight.current = true;
    setBusy(true);
    setMessages(initial);
    const eventsBefore = memorySessionRef.current === sid && memory ? memory.event_count : 0;
    try {
      const result = await consumeTurn(
        controller, (signal) => chatApi.reattach(aid, sid, turn, 0, signal), turn, null, [], sid,
      );
      if (result.outcome.kind === "aborted") return;
      if (result.completed && !result.failed && memoryState !== "off") void settleMemory(sid, eventsBefore);
      else void refreshMemory(sid);
    } catch (err) {
      if (controller.signal.aborted) return;
      // another member's turn (409) or a vanished turn: the history shown is the truth
      if (!(err instanceof ApiError && err.code === "chat.turn_not_yours")) toast("error", errorMessage(err));
    } finally {
      setMessages((m) => m.map((msg) => (msg.streaming ? { ...msg, streaming: false, reconnecting: false } : msg)));
      sendInFlight.current = false;
      setBusy(false);
      if (abortRef.current === controller) abortRef.current = null;
      void loadSessions(aid);
    }
  };

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
    // Count the rail showed before this turn (0 for a brand-new session).
    const eventsBefore = sessionId && memory ? memory.event_count : 0;
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const request: ChatRequest = { prompt, session_id: sessionId };
      if (files.length) {
        request.attachments = await Promise.all(files.map(encodeAttachment)).catch(() => {
          throw new Error(t("chatPage.attachments.readFailed"));
        });
      }
      const open = async (signal: AbortSignal) => {
        const res = await chatApi.stream(agentId, request, signal);
        // Encoded bytes are request-only; release them while the response streams.
        delete request.attachments;
        if (!res.body) throw new Error(t("chatPage.streamInterrupted"));
        return res;
      };
      const result = await consumeTurn(controller, open, null, userMessage, files, sessionId);
      ({ failed, completed, activeSessionId } = result);
      if (result.outcome.kind === "aborted") return;
      if (completed && !failed && !retrying) resetAttachments();
    } catch (err) {
      if (controller.signal.aborted) return;
      failed = true;
      const message = errorMessage(err);
      setMessages((m) => [...m, { kind: "error", text: message }]);
      if (files.length) setAttachmentError(message);
    } finally {
      if (failed && !retrying) setInput(prompt);
      setMessages((m) => m.map((msg) => (msg.streaming ? { ...msg, streaming: false, reconnecting: false } : msg)));
      sendInFlight.current = false;
      setBusy(false);
      if (abortRef.current === controller) abortRef.current = null;
      if (activeSessionId) {
        // A completed turn on a memory-bearing agent is what gets persisted;
        // anything else only needs the one read.
        if (completed && !failed && memoryState !== "off") void settleMemory(activeSessionId, eventsBefore);
        else void refreshMemory(activeSessionId);
      }
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
    memorySessionRef.current = null;
    setMessages([]);
    setMemory(null);
    resetTrace();
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

  const addFiles = (files: File[]) => {
    if (composerDisabled || !files.length) return;
    const additions = files.map((file) => ({ id: browserUuid(), file, mediaType: attachmentMediaType(file) }));
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
  // Only an *imported* managed Harness (N1 residue, R19) still has no session-stop
  // operation on AWS; a self-built Harness is Runtime-backed and stops like any
  // other method, so this does not gate on agent.method === "harness" anymore.
  const discoveredHarness = isHarnessAgent(agent);
  const currentEnded = Boolean(sessionId && sessions.find((s) => s.session_id === sessionId)?.ended_at);
  const endReason = (sid: string | null, ended: boolean): string | undefined =>
    !sid
      ? t("chatPage.endDisabledNoSession")
      : discoveredHarness
        ? t("chatPage.endDisabledDiscoveredHarness")
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
          {t(STALE_AGENT_COPY[staleAgent.state], {
            kind: t("staleLink.kind.agent"),
            id: staleAgent.id,
            status: t(`status.${staleAgent.status}`, { defaultValue: staleAgent.status }),
          })}
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
                  <Link className="v2-chat-extlink" to={`/v2/agents?view=detail&id=${encodeURIComponent(agent.id)}`}>
                    {t("v2.chat.agentDetail")}
                  </Link>
                </div>
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
              agentName={agent?.name}
              sessionId={sessionId}
              memory={memory}
              memoryState={memoryState}
              trace={traceFor === sessionId ? trace : null}
              traceBusy={traceBusy}
              traceError={traceError}
              onLoadTrace={() => void loadTrace()}
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

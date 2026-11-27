import { Plus, RefreshCw, Share2, Square } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { AttachmentHint } from "../../components/chat/AttachmentViews";
import { TurnStatusLine } from "../../components/chat/TurnStatus";
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
  type ChatSessionInfo,
  type ChatTraceInfo,
  type AuthRequiredEvent,
  errorMessage,
  feedbackApi,
  type FeedbackVerdict,
} from "../../lib/api";
import {
  chatErrorText,
  agentMemoryState,
  type AsUserChoice,
  asUserChecked,
  asUserField,
  AUTO_RETRIES,
  chatEligible,
  type ChatTurnRequest,
  connectionFailure,
  errorEventFailure,
  failureText,
  isHarnessAgent,
  isOutputEvent,
  isStoppedRow,
  lastUserIndex,
  latestTurnRetryable,
  requestFailure,
  restoredFailure,
  seconds,
  shouldAutoRetry,
  sseEvents,
  STALL_ABORT_MS,
  stopChatTurn,
  streamChat,
  toolRunById,
  type TurnFailure,
  type TurnStatus,
  turnStopReason,
  useChatTurn,
  waitForRetry,
} from "../../lib/chat";
import { editPath } from "../../lib/agent-admin/edit";
import { livePolicyDeny, restoredPolicyDeny } from "../../lib/policy-deny";
import {
  approvalActionable,
  approvalPrompt,
  decideApproval,
  liveApproval,
  restoredApproval,
  approvedToolsField,
} from "../../lib/tool-approval";
import { liveAuthAsk, restoredAuthAsk } from "../../lib/user-grants";
import { useLoad, useV2Toast } from "../hooks";
import { Alert, Button, Card, Confirm, LinkButton, PageHeader, Select, Spin, Tag } from "../ui";
import "./chat/chat.css";
import { Composer } from "./chat/Composer";
import { ShareLinksModal } from "./chat/ShareLinks";
import { Inspector, type InspectorTab } from "./chat/Inspector";
import { SessionRail } from "./chat/SessionRail";
import { appendDelta, attachStructured, latestRetryTarget } from "./chat/messages";
import { type ChatMessage, Thread } from "./chat/Thread";

/** The latest turn's retry offer (chat-streaming §4.1): the failure, the prompt to
 *  re-send and the turn's user row in the ledger (what `retry_of` names). */
interface RetryOffer {
  failure: TurnFailure;
  prompt: string;
  ledgerId?: number;
  /** SB-2: the tools the failed turn approved — its retry approves them again */
  approved?: string[];
}

const MEMORY_TAG = {
  on: "v2.chat.memoryOn",
  off: "v2.chat.memoryOff",
  "agent-defined": "v2.chat.memoryAgentDefined",
} as const;

/**
 * 对话调试 — live SSE chat with any active agent, over the same invoke chain as
 * the public `/v1` API. Deep links: `?agent=<id>` preselects the agent,
 * `&session=<id>` replays and resumes that conversation, `&tab=api` opens the
 * inspector on its API panel (where keys are created).
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
  const [tab, setTab] = useState<InspectorTab>(() => (params.get("tab") === "api" ? "api" : params.get("tab") === "files" ? "files" : "trace"));
  const [sharing, setSharing] = useState(false);
  // chat-streaming S2: what the running turn is doing, its timer and its stop
  const turn = useChatTurn();
  const turnSeq = useRef(0);
  // chat-streaming S3: the retry the latest turn offers, the automatic retry under
  // way (`n` of `AUTO_RETRIES`) and the wait before it (the stop button cancels it)
  const [retryOffer, setRetryOffer] = useState<RetryOffer | null>(null);
  const [retrying, setRetrying] = useState<number | null>(null);
  const retryWait = useRef<AbortController | null>(null);
  // chat-streaming §5.2: the background turn the stream follows — the stop button ends it
  const liveTurn = useRef<string | undefined>(undefined);

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
      const restored = rows.map((r): ChatMessage =>
          r.role === "user"
            ? { kind: "user", text: r.text, attachments: r.attachments }
            : r.role === "agent"
              ? {
                kind: "agent", text: r.text, id: r.id, verdict: r.verdict ?? null, curated: !!r.answered_by,
                ...(r.structured != null ? { structured: r.structured } : {}),
              }
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
                    : r.role === "approval"
                      ? { kind: "approval", text: r.text, name: r.name ?? "", approval: restoredApproval(r) }
                    : isStoppedRow(r.text)
                      ? { kind: "error", text: t("v3.chat.status.stopped") }
                      : { kind: "error", text: failureText(t, restoredFailure(r.text)) },
        );
      setMessages(restored);
      // a latest turn that failed can be retried from here (one with files only says so)
      const user = rows[lastUserIndex(rows.map((r) => ({ kind: r.role })))];
      const last = rows[rows.length - 1];
      setRetryOffer(
        user && last?.role === "error" && latestTurnRetryable(restored)
          ? { failure: restoredFailure(last.text), prompt: user.text, ledgerId: user.id }
          : null,
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
  // composer's draft or its pending files. `retryOf` (the retry button,
  // chat-streaming §4.1): the latest turn is re-sent in place — its user message is
  // already in the thread — naming its ledger row, so the retry replaces it.
  // `approved` (an approval card's Approve, SB-2): the turn names those tools in
  // `approved_tools`, so the call the agent paused may run.
  const send = async (retryPrompt?: string, retryOf?: number | null, approved?: string[]) => {
    const retrying = retryPrompt !== undefined;
    const replacing = retryOf !== undefined;
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
    setRetryOffer(null);
    const userMessage: ChatMessage = {
      kind: "user",
      text: prompt,
      ...(approved?.length ? { approved } : {}),
      attachments: capability && files.length ? files.map((f) => attachmentMetadata(f, capability)) : undefined,
    };
    if (!replacing) setMessages((m) => [...m, userMessage]);
    let activeSessionId = sessionId;
    let replaces = retryOf ?? undefined;
    let failure: TurnFailure | null = null;
    let userLedgerId: number | undefined;

    // One attempt at the turn; returns how it failed (null: answered or stopped).
    const attemptTurn = async (): Promise<TurnFailure | null> => {
      let failed: TurnFailure | null = null;
      let completed = false;
      let produced = false; // a replay could repeat something the user saw
      const signal = turn.begin();
      liveTurn.current = undefined;
      const key = ++turnSeq.current;
      // a tool row follows its run in the turn status (ids repeat across turns)
      const syncRuns = (status: TurnStatus | null) =>
        setMessages((m) =>
          m.map((msg) => (msg.turn === key && msg.run ? { ...msg, run: toolRunById(status, msg.run.id) ?? msg.run } : msg)),
        );
      const fail = (next: TurnFailure) => {
        failed = next;
        const message = failureText(t, next);
        setMessages((m) => [...m, { kind: "error", text: message }]);
        if (files.length) setAttachmentError(message);
      };
      try {
        const request: ChatTurnRequest = { prompt, session_id: activeSessionId };
        if (replaces !== undefined) request.retry_of = replaces;
        const toolApprovals = approvedToolsField(approved);
        if (toolApprovals) request.approved_tools = toolApprovals;
        const asUserValue = asUserField(jwtAgent, asUser);
        if (asUserValue !== undefined) request.as_user = asUserValue;
        if (files.length) {
          request.attachments = await Promise.all(files.map(encodeAttachment)).catch(() => {
            throw new Error(t("chatPage.attachments.readFailed"));
          });
        }
        // the turn's signal also aborts the request itself (Stop before the first frame)
        const res = await streamChat(agentId, request, signal);
        // Encoded bytes are request-only; release them while the response streams.
        delete request.attachments;
        if (!res.body) throw new Error(t("chatPage.streamInterrupted"));
        let agentOpen = false;
        let curated = false; // T35: a `rule` event precedes a curated answer's text
        for await (const { event, data: payload } of sseEvents(res, { heartbeats: true, signal })) {
          const status = turn.feed(event, payload);
          if (isOutputEvent(event)) produced = true;
          if (event === "tool_end") {
            syncRuns(status);
          } else if (event === "meta") {
            liveTurn.current = payload.turn_id;
            if (payload.session_id) {
              activeSessionId = payload.session_id;
              setSessionId(payload.session_id);
              // keep the session in the URL so a reload restores this conversation
              setParams({ agent: agentId, session: payload.session_id }, { replace: true });
            }
            if (payload.user_message_id !== undefined) userLedgerId = payload.user_message_id;
            setCaller(payload.inbound?.caller ?? null);
            if (payload.attachments) {
              setMessages((m) =>
                m.map((msg) => (msg === userMessage ? { ...msg, attachments: payload.attachments } : msg)),
              );
            }
          } else if (event === "rule") {
            curated = true;
          } else if (event === "tool") {
            const run = status?.tools[status.tools.length - 1];
            setMessages((m) => [...m, { kind: "tool", text: payload.name ?? "tool", name: payload.name, run, turn: key }]);
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
          } else if (event === "approval_required" || event === "approval_refused") {
            // SB-2: the agent paused a listed tool call (or the runtime refused it)
            const approval = liveApproval(payload, event === "approval_refused");
            setMessages((m) => [...m, { kind: "approval", text: approval.tool, name: approval.tool, approval }]);
            agentOpen = false;
          } else if (event === "delta") {
            const open = agentOpen;
            setMessages((m) => appendDelta(m, payload.text ?? "", open, curated));
            agentOpen = true;
          } else if (event === "structured") {
            // SB-1: the parsed answer, shown under the text answer
            setMessages((m) => attachStructured(m, payload.value));
          } else if (event === "saved") {
            // the answer bubble just closed: attach its ledger id so it can be rated
            const messageId = payload.message_id;
            setMessages((m) => {
              const next = [...m];
              for (let i = next.length - 1; i >= 0; i--) {
                if (next[i].kind === "agent") {
                  if (next[i].id == null) next[i] = { ...next[i], id: messageId };
                  break;
                }
              }
              return next;
            });
          } else if (event === "error") {
            syncRuns(status);
            fail(errorEventFailure(payload, chatErrorText(payload, t("chatPage.sendFailed"))));
          } else if (event === "done") {
            completed = true;
            syncRuns(status);
            // Only an agent whose spec turns memory on persists the turn; the
            // console itself never writes memory.
            if (!failed && memoryState === "on") setMessages((m) => [...m, { kind: "memory", text: "" }]);
          }
        }
        const reason = turnStopReason(signal);
        if (reason === "stop" && !completed && !failed) {
          setMessages((m) => [...m, { kind: "error", text: t("v3.chat.status.stopped") }]);
        } else if (reason === "stalled") {
          fail(connectionFailure(produced, t("v3.chat.status.lost", { s: seconds(STALL_ABORT_MS) })));
        } else if (!completed && !failed) {
          fail(connectionFailure(produced, ""));
        }
        if (completed && !failed && !retrying) resetAttachments();
      } catch (err) {
        const reason = turnStopReason(signal);
        if (reason === "stop" && !completed && !failed) {
          // stopped before the stream opened: the fetch rejected with the abort
          setMessages((m) => [...m, { kind: "error", text: t("v3.chat.status.stopped") }]);
        } else if (reason === "stalled") {
          fail(connectionFailure(produced, t("v3.chat.status.lost", { s: seconds(STALL_ABORT_MS) })));
        } else if (!failed) {
          fail(requestFailure(err, produced));
        }
      } finally {
        // a call the stream never ended stopped with the turn
        if (!completed) syncRuns(turn.feed("error", {}));
        turn.end();
        setMessages((m) => m.map((msg) => (msg.streaming ? { ...msg, streaming: false } : msg)));
      }
      return failed;
    };

    try {
      for (let attempt = 0; ; attempt++) {
        failure = await attemptTurn();
        // files are not re-sent by themselves: a turn with attachments is the user's to repeat
        if (files.length || !shouldAutoRetry(failure, attempt)) break;
        // chat-streaming §4.2: the failed attempt gives way to the retry notice
        setMessages((m) => m.slice(0, lastUserIndex(m) + 1));
        setRetrying(attempt + 1);
        const wait = new AbortController();
        retryWait.current = wait;
        const go = await waitForRetry(wait.signal);
        retryWait.current = null;
        setRetrying(null);
        if (!go) {
          failure = null;
          setMessages((m) => [...m, { kind: "error", text: t("v3.chat.status.stopped") }]);
          break;
        }
        replaces = userLedgerId ?? replaces;
      }
      // a turn with files gets the offer too: its error item says to re-send the files
      if (failure?.manual) {
        setRetryOffer({ failure, prompt, ledgerId: userLedgerId ?? replaces, approved });
      }
    } finally {
      // a failure the retry offer does not cover hands the prompt back for editing
      if (failure && !retrying && (!failure.manual || files.length)) setInput(prompt);
      sendInFlight.current = false;
      setBusy(false);
      if (activeSessionId) void refreshMemory(activeSessionId);
      if (agentId) void loadSessions(agentId);
    }
  };

  // The retry button: the latest turn's items after its user message give way to
  // the retry, which names that turn's ledger row.
  const retryTurn = () => {
    if (!retryOffer || busy || latestRetryTarget(messages)?.attachments) return;
    const { prompt, ledgerId, approved } = retryOffer;
    setMessages((m) => m.slice(0, lastUserIndex(m) + 1));
    void send(prompt, ledgerId ?? null, approved);
  };

  // SB-2 approval card: Approve answers the paused call with the next turn; Decline
  // sends nothing — the call stays unrun and the composer is the user's.
  const approveTool = (index: number) => {
    const card = messages[index]?.approval;
    if (!card || busy || restoring || !approvalActionable(messages, index)) return;
    setMessages((m) => decideApproval(m, index, "approved"));
    void send(approvalPrompt(t, card.tool), undefined, [card.tool]);
  };
  const declineTool = (index: number) => {
    if (busy || !approvalActionable(messages, index)) return;
    setMessages((m) => decideApproval(m, index, "declined"));
  };

  // Thumbs: optimistic, reverted (with the reason) when the backend refuses.
  const rate = async (index: number, verdict: FeedbackVerdict | "none") => {
    const target = messages[index];
    if (!agentId || !sessionId || target?.kind !== "agent" || target.id == null) return;
    const previous = target.verdict ?? null;
    const apply = (value: FeedbackVerdict | null) =>
      setMessages((m) => m.map((msg, i) => (i === index ? { ...msg, verdict: value } : msg)));
    apply(verdict === "none" ? null : verdict);
    try {
      await feedbackApi.rate(agentId, { session_id: sessionId, message_id: target.id, verdict });
    } catch (err) {
      apply(previous);
      toast("error", errorMessage(err));
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
    setRetryOffer(null);
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
  // Stop: close the stream and end the background turn on the server (it no longer
  // ends with the connection, §5.2); a code runtime's session is stopped too — a
  // Harness has no session-stop operation.
  const stopTurn = () => {
    retryWait.current?.abort(); // a pending automatic retry is not made
    turn.stop("stop");
    if (agentId && liveTurn.current) void stopChatTurn(agentId, liveTurn.current).catch(() => undefined);
    if (agentId && sessionId && !harness) void api.stopChatSession(agentId, sessionId).catch(() => undefined);
  };

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
                    // T04: the human label first, the immutable slug alongside
                    ...agents.map((a) => ({
                      value: a.id,
                      label: a.display_name ? `${a.display_name} (${a.name})` : a.name,
                    })),
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
                  disabled={!agent}
                  onClick={() => setSharing(true)}
                  testId="share-agent"
                >
                  <Share2 size={12} aria-hidden="true" />
                  {t("shareLinks.button")}
                </Button>
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
              onRate={(index, verdict) => void rate(index, verdict)}
              onRetry={(prompt) => void send(prompt)}
              retryDisabled={busy || restoring}
              onRetryTurn={retryOffer && !busy && !restoring ? retryTurn : undefined}
              onApprove={approveTool}
              onDecline={declineTool}
              now={turn.now}
            />
            {retrying !== null && (
              <Alert tone="warn">{t("v3.chat.retry.retrying", { n: retrying, total: AUTO_RETRIES })}</Alert>
            )}
            {turn.status && <TurnStatusLine status={turn.status} now={turn.now} onStop={stopTurn} testId="chat-turn-status" />}
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
              placeholder={agent ? t("chatPage.placeholder", { name: agent.display_name || agent.name }) : t("chatPage.pickAgent")}
            />
          </Card>
        </div>

        <div className="v2-chat-pane v2-chat-pane-side">
          <Card>
            <Inspector
              tab={tab}
              onTab={setTab}
              agentId={agentId}
              editTo={agent ? editPath(agent) : ""}
              busy={busy}
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
      {sharing && agent && (
        <ShareLinksModal
          agentId={agent.id}
          agentName={agent.display_name || agent.name}
          onClose={() => setSharing(false)}
        />
      )}
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

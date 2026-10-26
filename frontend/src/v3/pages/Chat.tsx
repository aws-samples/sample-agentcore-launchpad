import { ArrowUp, Plus, RotateCcw, ScanSearch, ShieldX, Square, ThumbsDown, ThumbsUp, Wrench } from "lucide-react";
import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { ErrorCallout } from "../../components/ErrorCallout";
import { JumpToLatest } from "../../components/JumpToLatest";
import { Markdown } from "../../components/Markdown";
import { StructuredAnswer } from "../../components/chat/StructuredAnswer";
import { ToolApprovalCard } from "../../components/chat/ToolApprovalCard";
import {
  api,
  type ChatHistoryMessage,
  type ChatSessionInfo,
  feedbackApi,
  type FeedbackVerdict,
} from "../../lib/api";
import {
  activityLabel,
  agentMemoryState,
  AUTO_RETRIES,
  chatEligible,
  chatErrorText,
  chatHistory,
  connectionFailure,
  errorEventFailure,
  failureText,
  isHarnessAgent,
  isOutputEvent,
  isStoppedRow,
  isTurnGone,
  lastUserIndex,
  requestFailure,
  RESUME_DELAYS_MS,
  restoredFailure,
  resumeChat,
  resumeDelay,
  rowsBeforeTurn,
  type RunningTurn,
  seconds,
  shouldAutoRetry,
  sseEvents,
  STALL_ABORT_MS,
  stopChatTurn,
  streamChat,
  type TurnFailure,
  TOOL_RUN_GLYPH,
  toolRunById,
  toolRunLabel,
  turnActivity,
  turnElapsedMs,
  type TurnStatus,
  turnStopReason,
  useChatTurn,
  waitForRetry,
} from "../../lib/chat";
import { useStickToBottom } from "../../lib/useStickToBottom";
import { livePolicyDeny, type PolicyDeny, policyDenyHref, restoredPolicyDeny } from "../../lib/policy-deny";
import {
  approvalActionable,
  approvalPhase,
  approvalPrompt,
  decideApproval,
  liveApproval,
  restoredApproval,
  approvedToolsField,
} from "../../lib/tool-approval";
import { useAuth } from "../../auth/auth-context";
import { useWorkspace } from "../../workspace/workspace-context";
import { appendDelta, attachStructured } from "../../v2/pages/chat/messages";
import type { ChatMessage } from "../../v2/pages/chat/messages";
import { useFailure } from "../failure";
import { Reasoned } from "../reason";
import { stepReason } from "../reasons";
import { agentSignal } from "../signals";
import { Btn, Empty, Lamp, Notice, Skeleton } from "../ui";
import { useLoad } from "../hooks";
import { ago } from "../format";
import { StoryHeader } from "../story/StoryHeader";
import { ApiKeysEntry } from "./agent/ApiKeysCard";
import { Select } from "../select";
import { Inspector } from "./chat/Inspector";
import { type InspectTab, inspectTab } from "./chat/inspect";
import "./chat.css";
import "./chatStatus.css";

/** A thread item; a tool row of a live turn carries its run (status, duration),
 *  tagged with the turn it belongs to (`ChatMessage.run` / `.turn`).
 *  `note`: a neutral notice (the user stopped). `ledgerId`: a user message's ledger
 *  row (what a retry names). `failure`: an error item's cause and retry offer. */
type Msg = ChatMessage & { note?: boolean; ledgerId?: number; failure?: TurnFailure };

/** How one attempt at a turn ended: its failure (null: answered or stopped), the
 *  session it ran in and its user message's ledger row. */
interface Attempt {
  failure: TurnFailure | null;
  session?: string;
  userMessageId?: number;
}

/**
 * Chat, rebuilt around the conversation. The same SSE invoke chain as V2 and the
 * public /v1 API (one place changes invoke behaviour), drawn as a focused thread:
 * sessions on the left, the conversation in a single readable column, the composer
 * always in reach (⏎ to send, ⇧⏎ for a new line). Attachments and as_user consent
 * are not rebuilt here yet — the thread says so and hands that turn to V2.
 */
/**
 * A Gateway tool call a Cedar policy denied for the signed-in identity: the agent
 * reached the tool, the Gateway refused it. No retry — a deny holds until the
 * rule changes; the link goes to the policies that decide it.
 */
function PolicyDenied({ deny }: { deny: PolicyDeny }) {
  const { t } = useTranslation();
  const { authRequired, username, role } = useAuth();
  return (
    <div className="v3-deny" data-testid="policy-deny-card">
      <div className="head">
        <ShieldX size={15} aria-hidden="true" />
        <b>{t("v2.chat.policy.title")}</b>
        <span className="mono">{deny.tool || "—"}</span>
        <span className="v3-chip" data-s="act">{t("v2.chat.policy.denied")}</span>
      </div>
      <p>{t("v2.chat.policy.body", { tool: deny.tool || "—" })}</p>
      <dl className="v3-kv">
        <dt>{t("v2.chat.policy.reason")}</dt>
        <dd data-testid="policy-deny-reason">{deny.reason || "—"}</dd>
        {deny.policyId && (<><dt>{t("v2.chat.policy.policy")}</dt><dd className="mono">{deny.policyId}</dd></>)}
        {authRequired && username && (<><dt>{t("v2.chat.policy.identity")}</dt><dd><span className="mono">{username}</span>{role ? ` · ${role}` : ""}</dd></>)}
      </dl>
      <Link className="v3-btn sm" to={policyDenyHref(deny)} data-testid="policy-deny-link">
        {t(deny.gatewayId ? "v2.chat.policy.openPolicies" : "v2.chat.policy.openGovernance")}
      </Link>
    </div>
  );
}

export function V3Chat() {
  const { t } = useTranslation();
  const failed = useFailure();
  const { current } = useWorkspace();
  const [params, setParams] = useSearchParams();
  const agents = useLoad(() => api.listAgents(), `v3-chat-agents:${current?.id ?? ""}`);
  const eligible = useMemo(() => (agents.data ? chatEligible(agents.data.agents) : []), [agents.data]);
  const agentId = params.get("agent") ?? "";
  const agent = eligible.find((a) => a.id === agentId);
  const [sessionId, setSessionId] = useState<string | undefined>(params.get("session") ?? undefined);
  const [sessions, setSessions] = useState<ChatSessionInfo[]>([]);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [needsV2, setNeedsV2] = useState(false);
  // chat-streaming S3: the automatic retry under way (`n` of `AUTO_RETRIES`), and
  // the wait before it — the stop button cancels that wait
  const [retrying, setRetrying] = useState<number | null>(null);
  const retryWait = useRef<AbortController | null>(null);
  // chat-streaming S4: reconnecting to the running turn (`n` of the attempts), and
  // the turn the stream follows — what the stop button ends on the server
  const [resuming, setResuming] = useState<number | null>(null);
  const liveTurn = useRef<string | undefined>(undefined);
  // chat-streaming S2: what the running turn is doing, its timer and its stop
  const turn = useChatTurn();
  const turnSeq = useRef(0);
  // follows the stream only while the reader is at the bottom
  const thread = useStickToBottom();
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // the right rail (trace / memory): its open tab is `?inspect=`, absent = closed
  const inspect = inspectTab(params.get("inspect"));
  const inspecting = inspect !== null && agent !== undefined;
  // every navigation inside the page keeps the open rail
  const nav = (next: Record<string, string>, replace = true) =>
    setParams((prev) => {
      const tab = prev.get("inspect");
      return tab ? { ...next, inspect: tab } : next;
    }, { replace });
  const setInspect = (tab: InspectTab | null) =>
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      if (tab) next.set("inspect", tab);
      else next.delete("inspect");
      return next;
    }, { replace: true });

  // the first eligible agent is the default, so the page is never empty on arrival
  useEffect(() => {
    if (!agentId && eligible.length) {
      setParams((prev) => {
        const next: Record<string, string> = { agent: eligible[0].id };
        const tab = prev.get("inspect");
        if (tab) next.inspect = tab;
        return next;
      }, { replace: true });
    }
  }, [agentId, eligible, setParams]);

  const loadSessions = (aid: string) =>
    api.listChatSessions(aid).then((r) => setSessions(r.sessions)).catch(() => setSessions([]));

  useEffect(() => {
    if (agentId) void loadSessions(agentId);
  }, [agentId]);

  // a consent ask is not replayed (its URL is single-use): the next turn asks again
  const restoredThread = (rows: ChatHistoryMessage[]): Msg[] =>
    rows.filter((r) => r.role !== "auth").map((r): Msg =>
      r.role === "user"
        ? { kind: "user", text: r.text, ledgerId: r.id }
        : r.role === "agent"
          ? {
            kind: "agent", text: r.text, id: r.id, verdict: r.verdict ?? null, curated: !!r.answered_by,
            ...(r.structured != null ? { structured: r.structured } : {}),
          }
          : r.role === "tool"
            ? { kind: "tool", text: r.name ?? "tool", name: r.name ?? "tool" }
            : r.role === "policy"
              ? { kind: "policy", text: r.text, name: r.name ?? "", policy: restoredPolicyDeny(r) }
              : r.role === "approval"
                ? { kind: "approval", text: r.text, name: r.name ?? "", approval: restoredApproval(r) }
                : restoredError(r.text),
    );

  const open = async (sid: string) => {
    if (!agentId || busy) return;
    setRestoring(true);
    setNeedsV2(false);
    let running: RunningTurn | null = null;
    try {
      failed.clear();
      const history = await chatHistory(agentId, sid);
      running = history.running_turn ?? null;
      // a turn still answering is shown up to its user message; the rest is replayed
      const rows = rowsBeforeTurn(history.messages, running);
      setMessages(restoredThread(rows));
      setSessionId(sid);
      nav({ agent: agentId, session: sid });
      thread.toBottom(); // an opened conversation starts at its latest turn
    } catch (err) {
      running = null;
      failed.fail(err);
    } finally {
      setRestoring(false);
    }
    if (running) await followTurn(running, sid);
  };

  // Reload / reopen (chat-streaming §5.2): pick up a turn still answering in the
  // background — its whole event stream is replayed, then followed live.
  const followTurn = async (running: RunningTurn, sid: string) => {
    setBusy(true);
    try {
      await streamTurn({ follow: running.turn_id, session: sid });
    } finally {
      setBusy(false);
      void loadSessions(agentId);
    }
  };

  // a deep link with ?session= back-fills the thread once
  const restored = useRef(false);
  useEffect(() => {
    if (!restored.current && agentId && sessionId && messages.length === 0) {
      restored.current = true;
      void open(sessionId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, sessionId]);

  const fresh = () => {
    setMessages([]);
    setSessionId(undefined);
    setNeedsV2(false);
    nav(agentId ? { agent: agentId } : {});
    inputRef.current?.focus();
  };

  // a restored error row offers a retry; its text is the §4.3 advice. A turn the
  // user stopped reads as stopped, as it did live.
  const restoredError = (text: string): Msg => {
    if (isStoppedRow(text)) return { kind: "error", text: t("v3.chat.status.stopped"), note: true };
    const failure = restoredFailure(text);
    return { kind: "error", text: failureText(t, failure), failure };
  };

  const send = async () => {
    const prompt = input.trim();
    if (!prompt || !agentId || busy) return;
    setInput("");
    setMessages((m) => [...m, { kind: "user", text: prompt }]);
    thread.toBottom(); // the reader's own turn is always in view
    await runTurn(prompt, undefined, true);
  };

  // Retry (chat-streaming §4.1): the latest turn is re-sent in the same session and
  // replaces the failed one — its user message stays, everything after it goes.
  const retry = async () => {
    if (!agentId || busy) return;
    const user = messages[lastUserIndex(messages)];
    if (!user) return;
    setMessages((m) => m.slice(0, lastUserIndex(m) + 1));
    thread.toBottom();
    await runTurn(user.text, user.ledgerId, false, user.approved);
  };

  // SB-2 approval card: Approve answers the paused call with the next turn, which
  // names the tool in `approved_tools` (one approval = one call); Decline sends
  // nothing — the call stays unrun and the composer is the user's.
  const approve = async (index: number) => {
    const card = messages[index]?.approval;
    if (!card || !agentId || busy || !approvalActionable(messages, index)) return;
    const prompt = approvalPrompt(t, card.tool);
    const approved = [card.tool];
    setMessages((m) => [...decideApproval(m, index, "approved"), { kind: "user", text: prompt, approved }]);
    thread.toBottom();
    await runTurn(prompt, undefined, false, approved);
  };
  const decline = (index: number) => {
    if (busy || !approvalActionable(messages, index)) return;
    setMessages((m) => decideApproval(m, index, "declined"));
    inputRef.current?.focus();
  };

  // One turn, retried once by itself when it failed transiently before showing
  // anything (§4.2); each retry names the attempt it replaces (`retry_of`).
  const runTurn = async (prompt: string, retryOf: number | undefined, fromComposer: boolean, approved?: string[]) => {
    setBusy(true);
    let session = sessionId;
    let replaces = retryOf;
    let failure: TurnFailure | null = null;
    try {
      for (let attempt = 0; ; attempt++) {
        const result = await streamTurn({ prompt, session, retryOf: replaces, approved });
        session = result.session ?? session;
        failure = result.failure;
        if (!shouldAutoRetry(failure, attempt)) break;
        // the failed attempt gives way to the retry notice; the user message stays
        setMessages((m) => m.slice(0, lastUserIndex(m) + 1));
        setRetrying(attempt + 1);
        const wait = new AbortController();
        retryWait.current = wait;
        const go = await waitForRetry(wait.signal);
        retryWait.current = null;
        setRetrying(null);
        if (!go) {
          failure = null;
          setMessages((m) => [...m, { kind: "error", text: t("v3.chat.status.stopped"), note: true }]);
          break;
        }
        replaces = result.userMessageId ?? replaces;
      }
    } finally {
      // a failure the retry button cannot fix hands the prompt back for editing
      if (failure && !failure.manual && fromComposer) setInput((current) => current || prompt);
      setBusy(false);
      if (session) void loadSessions(agentId);
      inputRef.current?.focus();
    }
  };

  /** One attempt at a turn — a new one (`prompt`), or a running one to follow
   *  (`follow`: its turn id, replayed from its first event). A dropped or silent
   *  connection reconnects to the turn (`RESUME_DELAYS_MS`) before it counts as a
   *  failure; a turn no longer held by the server is shown from the ledger. */
  const streamTurn = async ({ prompt = "", session, retryOf, follow, approved }: {
    prompt?: string; session?: string; retryOf?: number; follow?: string; approved?: string[];
  }): Promise<Attempt> => {
    let failure: TurnFailure | null = null;
    let completed = false;
    let produced = false; // a replay could repeat something the user saw
    let answered = false;
    const attempt: Attempt = { failure: null, session };
    let signal = turn.begin();
    liveTurn.current = follow;
    let lastSeq = 0;
    // the server no longer holds the turn / some of its events left its buffer: the
    // ledger is what is shown
    let gone = false;
    let gapped = false;
    const key = ++turnSeq.current;
    // a tool row follows its run in the turn status (ids repeat across turns)
    const syncRuns = (status: TurnStatus | null) =>
      setMessages((m) =>
        m.map((msg) => (msg.turn === key && msg.run ? { ...msg, run: toolRunById(status, msg.run.id) ?? msg.run } : msg)),
      );
    const fail = (next: TurnFailure) => {
      failure = next;
      setMessages((m) => [...m, { kind: "error", text: failureText(t, next), failure: next }]);
    };
    const stopped = () => setMessages((m) => [...m, { kind: "error", text: t("v3.chat.status.stopped"), note: true }]);
    let openBubble = false;
    let curated = false;
    // read one connection to the turn, to its end or until it drops
    const read = async (res: Response): Promise<void> => {
      if (!res.body) throw new Error(t("v3.chat.interrupted"));
      for await (const { event, data, seq } of sseEvents(res, { heartbeats: true, signal })) {
        if (seq !== undefined) lastSeq = seq;
        const status = turn.feed(event, data);
        if (isOutputEvent(event)) produced = true;
        if (event === "gap") {
          gapped = true;
        } else if (event === "tool_end") {
          syncRuns(status);
        } else if (event === "meta" && data.session_id) {
          liveTurn.current = data.turn_id ?? liveTurn.current;
          attempt.session = data.session_id;
          attempt.userMessageId = data.user_message_id;
          const ledgerId = data.user_message_id;
          setMessages((m) => {
            const at = lastUserIndex(m);
            return at < 0 ? m : m.map((msg, i) => (i === at ? { ...msg, ledgerId } : msg));
          });
          setSessionId(data.session_id);
          nav({ agent: agentId, session: data.session_id });
        } else if (event === "rule") {
          curated = true;
        } else if (event === "tool") {
          const run = status?.tools[status.tools.length - 1];
          setMessages((m) => [...m, { kind: "tool", text: data.name ?? "tool", name: data.name, run, turn: key }]);
          openBubble = false;
        } else if (event === "auth_required") {
          // consent cards are V2's for now: say so rather than half-render one
          setNeedsV2(true);
        } else if (event === "policy_denied") {
          // follows its tool row, which already closed the open bubble
          const policy = livePolicyDeny(data);
          setMessages((m) => [...m, { kind: "policy", text: policy.reason, name: policy.tool, policy }]);
        } else if (event === "approval_required" || event === "approval_refused") {
          // SB-2: the agent paused a listed tool call (or the runtime refused it); a
          // paused call is this turn's outcome, so it counts as an answer
          answered = true;
          const approval = liveApproval(data, event === "approval_refused");
          setMessages((m) => [...m, { kind: "approval", text: approval.tool, name: approval.tool, approval }]);
          openBubble = false;
        } else if (event === "delta") {
          const wasOpen = openBubble;
          answered = true;
          setMessages((m) => appendDelta(m, data.text ?? "", wasOpen, curated));
          openBubble = true;
        } else if (event === "structured") {
          // SB-1: the parsed answer, shown under the text answer (it counts as an answer)
          answered = true;
          setMessages((m) => attachStructured(m, data.value));
        } else if (event === "saved") {
          setMessages((m) => {
            const next = [...m];
            for (let i = next.length - 1; i >= 0; i--) {
              if (next[i].kind === "agent") {
                if (next[i].id == null) next[i] = { ...next[i], id: data.message_id };
                break;
              }
            }
            return next;
          });
        } else if (event === "error") {
          syncRuns(status);
          fail(errorEventFailure(data, chatErrorText(data, t("v3.chat.failed"))));
        } else if (event === "done") {
          completed = true;
          syncRuns(status);
        }
      }
    };
    // reconnect to the running turn after a drop; false: give up (stop, or no more tries)
    const resume = async (): Promise<boolean> => {
      const turnId = liveTurn.current;
      if (!turnId) return false;
      for (let tries = 0; ; tries++) {
        const delay = resumeDelay(tries);
        if (delay === null || turnStopReason(signal) === "stop") return false;
        setResuming(tries + 1);
        signal = turn.reconnect();
        const go = await waitForRetry(signal, delay);
        setResuming(null);
        if (!go) return false;
        try {
          await read(await resumeChat(agentId, turnId, lastSeq, signal));
          return true;
        } catch (err) {
          if (isTurnGone(err)) {
            gone = true;
            return false;
          }
          if (turnStopReason(signal) === "stop") return false;
        }
      }
    };
    try {
      // the turn's signal also aborts the request itself (Stop before the first frame)
      const first = follow
        ? resumeChat(agentId, follow, 0, signal)
        : streamChat(agentId, {
          prompt, session_id: session ?? null, retry_of: retryOf, approved_tools: approvedToolsField(approved),
        }, signal);
      try {
        await read(await first);
      } catch (err) {
        // a connection that broke mid-turn is resumed below; any other failure is the turn's
        if (!liveTurn.current || turnStopReason(signal) === "stop") throw err;
        gone = isTurnGone(err);
      }
      while (!completed && !failure && !gone && turnStopReason(signal) !== "stop") {
        if (!(await resume())) break;
      }
      if (attempt.session && ((gone && !completed && !failure) || (gapped && completed))) {
        // shown from the ledger: the answer it saved, or how it ended
        if (await showSaved(attempt.session)) return attempt;
      }
      const reason = turnStopReason(signal);
      if (reason === "stop" && !completed && !failure) {
        stopped();
      } else if (reason === "stalled") {
        fail(connectionFailure(produced, t("v3.chat.status.lost", { s: seconds(STALL_ABORT_MS) })));
      } else if (!completed && !failure) {
        fail(connectionFailure(produced, ""));
      } else if (completed && !failure && !answered) {
        // the turn ended without a word: it may be retried, never by itself
        fail({ category: "other", manual: true, auto: false, detail: t("v3.chat.retry.empty") });
      }
    } catch (err) {
      const reason = turnStopReason(signal);
      if (reason === "stop" && !completed && !failure) {
        // stopped before the stream opened: the fetch rejected with the abort
        stopped();
      } else if (reason === "stalled") {
        fail(connectionFailure(produced, t("v3.chat.status.lost", { s: seconds(STALL_ABORT_MS) })));
      } else if (!failure) {
        fail(requestFailure(err, produced));
      }
    } finally {
      setResuming(null);
      liveTurn.current = undefined;
      // a call the stream never ended stopped with the turn
      if (!completed) syncRuns(turn.feed("error", {}));
      turn.end();
      setMessages((m) => m.map((msg) => (msg.streaming ? { ...msg, streaming: false } : msg)));
    }
    attempt.failure = failure;
    return attempt;
  };

  /** A turn the server no longer holds (or one whose events were lost): show the
   *  session as the ledger saved it. True when the saved turn says how it ended (an
   *  answer or an error row); false when it holds neither. */
  const showSaved = async (sid: string): Promise<boolean> => {
    try {
      const rows = (await chatHistory(agentId, sid)).messages;
      setMessages(restoredThread(rows));
      const tail = rows.slice(rows.map((r) => r.role).lastIndexOf("user") + 1);
      return tail.some((r) => r.role === "agent" || r.role === "error");
    } catch {
      return false; // the ledger is out of reach too
    }
  };

  // Stop: close the stream and end the background turn on the server (§5.2 — the
  // turn no longer ends with the connection); a code runtime's session is stopped
  // too. A Harness has no session-stop operation.
  const stopTurn = () => {
    retryWait.current?.abort(); // a pending automatic retry is not made
    turn.stop("stop");
    if (agentId && liveTurn.current) void stopChatTurn(agentId, liveTurn.current).catch(() => undefined);
    if (agentId && sessionId && !isHarnessAgent(agent)) {
      void api.stopChatSession(agentId, sessionId).catch(() => undefined);
    }
  };
  const activity = turn.status ? turnActivity(turn.status, turn.now) : null;
  // only the latest turn can be retried (the backend refuses any other)
  const latestUser = lastUserIndex(messages);

  const rate = async (index: number, verdict: FeedbackVerdict | "none") => {
    const msg = messages[index];
    if (!msg?.id || !sessionId || !agentId) return;
    const before = msg.verdict ?? null;
    setMessages((m) => m.map((x, i) => (i === index ? { ...x, verdict: verdict === "none" ? null : verdict } : x)));
    try {
      failed.clear();
      await feedbackApi.rate(agentId, { session_id: sessionId, message_id: msg.id, verdict });
    } catch (err) {
      setMessages((m) => m.map((x, i) => (i === index ? { ...x, verdict: before } : x)));
      failed.fail(err);
    }
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void send();
    }
  };

  return (
    <div className={inspecting ? "v3-chat inspecting" : "v3-chat"}>
      <StoryHeader />
      <aside className="v3-chat-side">
        <label className="v3-stat" style={{ display: "block" }}>
          <span className="label">{t("v3.chat.agent")}</span>
          <Select
            className="v3-select"
            style={{ marginTop: 8 }}
            value={agentId}
            onChange={(e) => {
              setMessages([]);
              setSessionId(undefined);
              nav({ agent: e.target.value }, false);
            }}
          >
            {eligible.length === 0 && <option value="">{t("v3.chat.noAgents")}</option>}
            {eligible.map((a) => (
              <option key={a.id} value={a.id}>{a.display_name || a.name}</option>
            ))}
          </Select>
        </label>
        <Btn onClick={fresh} disabled={!agentId}
          disabledReason={eligible.length === 0 ? stepReason(t, "build.create") : t("story.reason.pickAgent")}>
          <Plus size={14} /> {t("v3.chat.new")}
        </Btn>
        <div className="v3-rail-label" style={{ padding: "6px 2px 0" }}>{t("v3.chat.sessions")}</div>
        <div className="v3-chat-sessions">
          {sessions.length === 0 && <span style={{ color: "var(--v3-text-3)", fontSize: 12 }}>{t("v3.chat.noSessions")}</span>}
          {sessions.map((s) => (
            <button key={s.session_id} type="button" className={`v3-chat-session${s.session_id === sessionId ? " on" : ""}`}
              onClick={() => void open(s.session_id)}>
              <span className="preview">{s.preview || s.session_id.slice(0, 12)}</span>
              <span className="meta mono">{s.turns} · {ago(s.last_at)}</span>
            </button>
          ))}
        </div>
        {agentId && <ApiKeysEntry agentId={agentId} />}
      </aside>

      <section className="v3-chat-main">
        <header className="v3-chat-head">
          {agent ? (
            <>
              <Lamp s={agentSignal(agent)} live={agent.status === "active"} />
              <b>{agent.display_name || agent.name}</b>
              <span className="mono" style={{ color: "var(--v3-text-3)" }}>{sessionId ? sessionId.slice(0, 16) : t("v3.chat.newSession")}</span>
              <Link to={`/v3/agents?id=${agent.id}`} className="v3-btn ghost sm" style={{ marginLeft: "auto" }}>
                {t("v3.chat.agentPage")}
              </Link>
              <button type="button" className={`v3-btn sm${inspect ? "" : " ghost"}`} aria-pressed={!!inspect}
                onClick={() => setInspect(inspect ? null : "trace")} data-testid="v3-chat-inspect-toggle">
                <ScanSearch size={14} /> {t("v3.chat.inspect.toggle")}
              </button>
            </>
          ) : (
            <span style={{ color: "var(--v3-text-3)" }}>{t("v3.chat.pick")}</span>
          )}
        </header>

        <div className="v3-chat-thread" ref={thread.ref}>
          {restoring ? (
            <Skeleton rows={5} />
          ) : messages.length === 0 ? (
            <Empty title={t("v3.chat.emptyTitle")}>{t("v3.chat.emptySub")}</Empty>
          ) : (
            messages.map((m, i) => {
              if (m.kind === "user") return <div key={i} className="v3-msg user"><div className="bubble">{m.text}</div></div>;
              if (m.kind === "tool")
                return (
                  <div key={i} className="v3-msg tool" data-run={m.run?.status} data-testid="chat-tool">
                    {m.run ? (
                      <span className="v3-tool-glyph" aria-hidden="true">{TOOL_RUN_GLYPH[m.run.status]}</span>
                    ) : (
                      <Wrench size={12} aria-hidden="true" />
                    )}{" "}
                    <span className="mono">{m.name ?? m.text}</span>
                    {m.run && <span className="v3-tool-state">{toolRunLabel(t, m.run, turn.now)}</span>}
                  </div>
                );
              if (m.kind === "error")
                return (
                  <div key={i} className="v3-msg" data-testid="chat-error" data-category={m.failure?.category}>
                    <Notice s={m.note ? "wait" : "act"}>
                      {m.text.split("\n").map((line, j) => (
                        <span key={j} style={{ display: "block" }}>{line}</span>
                      ))}
                      {m.failure?.manual && i > latestUser && !busy && (
                        <span style={{ display: "block", marginTop: 8 }}>
                          <Btn size="sm" onClick={() => void retry()}>
                            <RotateCcw size={13} aria-hidden="true" /> {t("v3.chat.retry.button")}
                          </Btn>
                        </span>
                      )}
                    </Notice>
                  </div>
                );
              if (m.kind === "policy" && m.policy) return <div key={i} className="v3-msg"><PolicyDenied deny={m.policy} /></div>;
              if (m.kind === "approval" && m.approval)
                return (
                  <div key={i} className="v3-msg">
                    <ToolApprovalCard
                      approval={m.approval}
                      phase={approvalPhase(m.approval, approvalActionable(messages, i))}
                      disabled={busy}
                      onApprove={() => void approve(i)}
                      onDecline={() => decline(i)}
                    />
                  </div>
                );
              if (m.kind !== "agent") return null;
              return (
                <div key={i} className={`v3-msg agent${m.streaming ? " streaming" : ""}`}>
                  <div className="bubble">
                    {m.text && <Markdown text={m.text} />}
                    {m.streaming && <span className="v3-caret" aria-hidden="true" />}
                    {m.structured !== undefined && <StructuredAnswer value={m.structured} />}
                  </div>
                  {m.id != null && !m.streaming && (
                    <div className="v3-msg-actions">
                      {m.curated && <span className="v3-chip" data-s="info">{t("v3.chat.curated")}</span>}
                      <button type="button" aria-pressed={m.verdict === "up"} aria-label={t("v3.chat.up")}
                        onClick={() => void rate(i, m.verdict === "up" ? "none" : "up")}>
                        <ThumbsUp size={13} />
                      </button>
                      <button type="button" aria-pressed={m.verdict === "down"} aria-label={t("v3.chat.down")}
                        onClick={() => void rate(i, m.verdict === "down" ? "none" : "down")}>
                        <ThumbsDown size={13} />
                      </button>
                    </div>
                  )}
                </div>
              );
            })
          )}
          {turn.status && activity && (
            <div className="v3-msg agent v3-working" data-kind={activity.kind} data-testid="chat-working" role="status">
              <div className="bubble">
                <span className="v3-working-dot" aria-hidden="true" />
                <span>{activityLabel(t, activity)}</span>
                <span className="mono v3-working-time">
                  {t("v3.chat.status.elapsed", { s: seconds(turnElapsedMs(turn.status, turn.now)) })}
                </span>
              </div>
            </div>
          )}
          {resuming !== null && (
            <div className="v3-msg" data-testid="chat-resuming">
              <Notice s="wait">{t("v3.chat.resume.reconnecting", { n: resuming, total: RESUME_DELAYS_MS.length })}</Notice>
            </div>
          )}
          {retrying !== null && (
            <div className="v3-msg" data-testid="chat-retrying">
              <Notice s="wait">{t("v3.chat.retry.retrying", { n: retrying, total: AUTO_RETRIES })}</Notice>
            </div>
          )}
          {needsV2 && (
            <div className="v3-msg">
              <Notice s="wait">
                {t("v3.chat.consentInV2")}{" "}
                <Link to={`/v2/chat?agent=${agentId}${sessionId ? `&session=${sessionId}` : ""}&full=1`} style={{ textDecoration: "underline" }}>
                  {t("v3.chat.openInV2")}
                </Link>
              </Notice>
            </div>
          )}
          {failed.error != null && (
            <div className="v3-msg">
              <ErrorCallout error={failed.error} params={{ agent_id: agentId || undefined }} onDismiss={failed.clear} data-testid="v3-chat-action-error" />
            </div>
          )}
          <JumpToLatest show={!thread.atBottom} onClick={thread.toBottom} />
        </div>

        <form
          className="v3-chat-composer"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <Reasoned disabledReason={!agent ? (eligible.length === 0 ? stepReason(t, "build.create") : t("story.reason.pickAgent")) : null}>
          <textarea
            ref={inputRef}
            rows={1}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKey}
            placeholder={agent ? t("v3.chat.placeholder", { name: agent.display_name || agent.name }) : t("v3.chat.pick")}
            disabled={!agent || busy}
            aria-label={t("v3.chat.placeholder", { name: agent?.name ?? "" })}
          />
          </Reasoned>
          {busy ? (
            <button type="button" className="v3-btn" onClick={stopTurn} aria-label={t("v3.chat.status.stop")}
              title={t("v3.chat.status.stop")} data-testid="chat-stop">
              <Square size={14} />
            </button>
          ) : (
            <Reasoned disabledReason={!agent ? t("story.reason.pickAgent") : t("story.reason.typePrompt")}>
              <button type="submit" className="v3-btn primary" disabled={!agent || !input.trim()} aria-label={t("v3.chat.send")}>
                <ArrowUp size={16} />
              </button>
            </Reasoned>
          )}
        </form>
      </section>
      {inspecting && inspect && agent && (
        <Inspector
          key={`${agentId}:${sessionId ?? ""}`}
          tab={inspect}
          onTab={setInspect}
          onClose={() => setInspect(null)}
          agentId={agentId}
          sessionId={sessionId}
          busy={busy || restoring}
          memoryState={agentMemoryState(agent)}
        />
      )}
    </div>
  );
}

/**
 * Chat turn link (N-ARCH-obs15 v2) — the connection is not the turn, for the Chat
 * playground too.
 *
 * A console turn (`POST /api/chat/{agent_id}`) runs server-side to its terminal event
 * whatever happens to the browser's connection (phone lock, app background, reload).
 * This module is the chat consumer of the shared reconnecting driver in
 * `lib/assistant-turn.ts`: the same backoff, wake-ups and give-up budget, with the chat
 * reattach endpoint (`GET …/sessions/{sid}/turns/{turn}/stream?from=`) and the chat
 * refusal codes; plus the pure helpers both Chat pages (classic and V2) use to mark the
 * open bubble as reconnecting, close it when the link gives up, and plan a page-load
 * reattach from `history.live_turn`. Nothing here renders.
 */
import { ApiError, type ChatLiveTurn, chatApi } from "./api";
import { isAssistantUnauthorized } from "./assistant";
import { runTurnLink, type TurnLinkOptions, type TurnOutcome } from "./assistant-turn";

/** Refusals of the chat reattach itself: final (no retry). Anything else — a network
 *  failure, a 5xx — is retried until the claim TTL has passed. */
export function isFinalChatReattachError(err: unknown): boolean {
  return (
    err instanceof ApiError &&
    (err.code === "chat.turn_not_found" ||
      err.code === "chat.turn_not_yours" ||
      err.code === "chat.session_not_found" ||
      err.code === "chat.invalid_seq" ||
      err.code === "agent.not_found" ||
      isAssistantUnauthorized(err))
  );
}

export interface ChatTurnLinkOptions
  extends Omit<TurnLinkOptions, "reattach" | "isFinalError" | "lostCode"> {
  agentId: string;
  /** the session the turn belongs to — known up front for a continuing session, only
   *  after `meta` for a new one (the page records it from the `meta` payload) */
  sessionId: () => string | null;
}

/**
 * Consume one console turn to its terminal event, reattaching across connection losses.
 * `open` is the `POST /api/chat/{agent_id}` response (a new turn) or the reattach itself
 * (a page load with `live_turn`). The first `open` failing throws exactly as before.
 */
export function runChatTurnLink(opts: ChatTurnLinkOptions): Promise<TurnOutcome> {
  const { agentId, sessionId, ...rest } = opts;
  return runTurnLink({
    ...rest,
    reattach: (turn, fromSeq, signal) => {
      const sid = sessionId();
      if (!sid) {
        return Promise.reject(
          new ApiError("chat.turn_lost", "the stream closed before the session was known", null),
        );
      }
      return chatApi.reattach(agentId, sid, turn, fromSeq, signal);
    },
    isFinalError: isFinalChatReattachError,
    lostCode: "chat.turn_lost",
  });
}

// ─── transcript helpers (pure; shared by the classic and V2 Chat pages) ────

interface BubbleLike {
  kind: string;
  text: string;
  streaming?: boolean;
  reconnecting?: boolean;
}

/** Index of the open answer bubble: the last `agent` row that is still streaming,
 *  skipping the consent / policy cards that sit after it without closing it. */
function openBubbleIndex<T extends BubbleLike>(messages: T[]): number {
  let i = messages.length - 1;
  while (i >= 0 && (messages[i].kind === "auth" || messages[i].kind === "policy")) i -= 1;
  return i >= 0 && messages[i].kind === "agent" && messages[i].streaming ? i : -1;
}

/**
 * Append a streamed `delta` — the one delta reducer of both Chat pages. `open` is the
 * page's "a bubble of THIS turn is open" flag (false before the first delta and after a
 * `tool` row closed the bubble): with it the text joins the open bubble, skipping a
 * consent / policy card asked mid-answer, which sits after the bubble without closing
 * it; without it a new streaming bubble is pushed. Nothing is ever appended to a
 * finished bubble from an earlier turn.
 */
export function appendDelta<T extends BubbleLike>(messages: T[], text: string, open: boolean): T[] {
  const next = [...messages];
  const at = openBubbleIndex(next);
  if (open && at >= 0) {
    next[at] = { ...next[at], text: next[at].text + text };
  } else {
    next.push({ kind: "agent", text, streaming: true } as T);
  }
  return next;
}

/**
 * Flip the open bubble's `reconnecting` flag. While reconnecting with no bubble open yet
 * (the connection dropped before the first delta) an empty streaming placeholder carries
 * the flag; clearing the flag removes such an empty placeholder again (the replay from
 * the last `seq` rebuilds the real bubble).
 */
export function markChatReconnecting<T extends BubbleLike>(messages: T[], reconnecting: boolean): T[] {
  const at = openBubbleIndex(messages);
  if (reconnecting) {
    if (at >= 0) {
      if (messages[at].reconnecting) return messages;
      const next = [...messages];
      next[at] = { ...next[at], reconnecting: true };
      return next;
    }
    return [...messages, { kind: "agent", text: "", streaming: true, reconnecting: true } as T];
  }
  if (at < 0) return messages;
  if (!messages[at].text && messages[at].reconnecting) return messages.filter((_m, i) => i !== at);
  if (!messages[at].reconnecting) return messages;
  const next = [...messages];
  next[at] = { ...next[at], reconnecting: false };
  return next;
}

/** The transcript after the link gave up: every streaming bubble is closed (an empty
 *  one dropped) and the interrupted copy is shown as an error line. */
export function giveUpChatMessages<T extends BubbleLike>(messages: T[], text: string): T[] {
  return [
    ...messages
      .filter((m) => !(m.kind === "agent" && m.streaming && !m.text))
      .map((m) => (m.streaming ? { ...m, streaming: false, reconnecting: false } : m)),
    { kind: "error", text } as T,
  ];
}

export interface ChatReattachPlan<T> {
  turn: number;
  /** the transcript to show while replaying from seq 0: the live turn's own rows after
   *  its user row are dropped (the replay rebuilds them, exactly as a just-sent turn
   *  builds its rows from the first `delta` / `tool` event; the page's busy state is
   *  the activity signal until then — no placeholder bubble, which the replay's first
   *  `delta` would not adopt, leaving an empty row above the real answer) */
  messages: T[];
}

/**
 * What a page load does with `history.live_turn`: reattach from seq 0 when the turn is
 * the viewer's (or the viewer is unknown — the server refuses another member's turn
 * with 409 anyway); nothing otherwise. One turn runs per session, so the live turn's
 * rows are the trailing rows after the last `user` row.
 */
export function chatReattachPlan<T extends BubbleLike>(
  rows: T[],
  live: ChatLiveTurn | null | undefined,
  viewer: string | null | undefined,
): ChatReattachPlan<T> | null {
  if (!live) return null;
  if (viewer && live.author && live.author !== viewer) return null;
  let lastUser = rows.length - 1;
  while (lastUser >= 0 && rows[lastUser].kind !== "user") lastUser -= 1;
  return { turn: live.turn, messages: lastUser >= 0 ? rows.slice(0, lastUser + 1) : rows };
}

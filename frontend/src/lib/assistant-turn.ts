/**
 * Architect turn link (N-ARCH-obs15) — the connection is not the turn.
 *
 * A turn runs server-side to its terminal row whatever happens to the browser's
 * connection (phone lock, app background, scope change, reload). This module is the
 * client half, shared by the classic `/create/assistant` page and the V2 assistant
 * page: the pure transcript reducer for one SSE event, and the reconnecting stream
 * driver — open (or reattach), consume, and on a close without a terminal event
 * reattach from the last seen `seq` with exponential backoff until the server's claim
 * TTL has passed. Nothing here renders; DOM wake-ups are injected (`domWait`).
 * `runTurnLink` is domain-neutral: the Chat pages drive it through `lib/chat-turn.ts`
 * (v2 of the same change) with their own reattach endpoint and refusal codes.
 */
import { ApiError, type AssistantConversationDetail, type AssistantProposal, type ValidationErrorDetail } from "./api";
import {
  type AssistantLiveMessage,
  PROPOSAL_AUTOCORRECT_FAILED,
  PROPOSAL_AUTOCORRECT_PENDING,
  PROPOSAL_AUTOCORRECTED,
  PROPOSAL_MISSING_PENDING,
  PROPOSAL_MISSING_RECOVERED,
  PROPOSAL_REJECTED_NAME,
  type SseFrame,
  isAssistantUnauthorized,
  sseEvents,
  TURN_INTERRUPTED_NAME,
  toLiveMessages,
} from "./assistant";

export type TurnEvent = SseFrame;

/** First retry after 1 s, doubling, never longer than 30 s between attempts. */
export const TURN_RECONNECT_BASE_MS = 1000;
export const TURN_RECONNECT_CAP_MS = 30_000;
/** Backend `TURN_CLAIM_TTL_S`: past it the claim is an orphan and the turn will never
 *  end on its own — stop reattaching and show the interrupted copy. */
export const TURN_RECONNECT_GIVE_UP_MS = 1_800_000;

export function reconnectDelay(attempt: number): number {
  return Math.min(TURN_RECONNECT_BASE_MS * 2 ** Math.max(0, attempt), TURN_RECONNECT_CAP_MS);
}

// ─── transcript reducer ───────────────────────────────────────────────────

const STREAMING_ROW: AssistantLiveMessage = { role: "assistant", text: "", streaming: true };

/** Flip the streaming row's `reconnecting` flag (the only row that has one). */
export function markReconnecting(
  messages: AssistantLiveMessage[],
  reconnecting: boolean,
): AssistantLiveMessage[] {
  return messages.map((m) =>
    m.role === "assistant" && m.streaming && Boolean(m.reconnecting) !== reconnecting
      ? { ...m, reconnecting }
      : m,
  );
}

/**
 * One SSE event applied to the live transcript. Pure: the pages call it inside
 * `setMessages`. `translateError(code, message)` renders an `error` event's text.
 */
export function applyTurnEvent(
  messages: AssistantLiveMessage[],
  evt: TurnEvent,
  translateError: (code: string | null, message: string) => string,
): AssistantLiveMessage[] {
  const data = evt.data as Record<string, unknown>;
  switch (evt.event) {
    case "delta": {
      const copy = [...messages];
      const last = copy[copy.length - 1];
      if (last?.role === "assistant" && last.streaming) {
        copy[copy.length - 1] = { ...last, text: last.text + String(data.text ?? ""), reconnecting: false };
      }
      return copy;
    }
    case "tool": {
      const copy = [...messages];
      const last = copy[copy.length - 1];
      const streaming = last?.role === "assistant" && last.streaming;
      if (streaming) copy.pop();
      copy.push({ role: "tool", text: "", name: String(data.name ?? "") });
      copy.push(streaming ? { ...last, reconnecting: false } : { ...STREAMING_ROW });
      return copy;
    }
    case "retry": {
      // the server replays the turn in a fresh session after a transient upstream
      // failure: drop what the failed attempt streamed (text and tool cards) — for the
      // automatic correction call only what IT streamed, after its notice (N17-obs295)
      // (N17-obs468: the same for the re-ask of a reply without a proposal block)
      const pendingName = data.phase === "autocorrect" ? PROPOSAL_AUTOCORRECT_PENDING
        : data.phase === "reask" ? PROPOSAL_MISSING_PENDING : null;
      let k = messages.length - 1;
      while (k >= 0 && !(pendingName
        ? messages[k].role === "error" && messages[k].name === pendingName
        : messages[k].role === "user")) k--;
      return [...messages.slice(0, k + 1), { ...STREAMING_ROW }];
    }
    case "autocorrect": {
      // N17-obs295: the first proposal failed only structural evaluation-plan rules; the
      // server corrects it with one internal call — close the reply, show the notice,
      // stream the correction below it
      const errors = Array.isArray(data.errors) ? data.errors.map(String) : [];
      const details = Array.isArray(data.validation_error_details)
        ? (data.validation_error_details as ValidationErrorDetail[])
        : undefined;
      return [
        ...messages
          .filter((x) => !(x.role === "assistant" && x.streaming && !x.text))
          .map((x) => (x.streaming ? { ...x, streaming: false, reconnecting: false } : x)),
        { role: "error", name: PROPOSAL_AUTOCORRECT_PENDING, text: errors.map((e) => `- ${e}`).join("\n"),
          ...(details ? { errors: details } : {}) },
        { ...STREAMING_ROW },
      ];
    }
    case "reask": {
      // N17-obs468: the reply carried no proposal block although it claimed a change; the
      // server asks once more — close the reply, show the notice, stream the follow-up
      const base = typeof data.revision === "number" ? data.revision : null;
      return [
        ...messages
          .filter((x) => !(x.role === "assistant" && x.streaming && !x.text))
          .map((x) => (x.streaming ? { ...x, streaming: false, reconnecting: false } : x)),
        { role: "error", name: PROPOSAL_MISSING_PENDING, text: "", missing: { base, revision: null } },
        { ...STREAMING_ROW },
      ];
    }
    case "tool_input": {
      // fills the arguments of the most recent tool card still waiting for them
      const input = String(data.input ?? "");
      if (!input) return messages;
      const copy = [...messages];
      for (let k = copy.length - 1; k >= 0; k--) {
        const row = copy[k];
        if (row.role === "tool" && !row.text) {
          copy[k] = { ...row, text: input };
          break;
        }
      }
      return copy;
    }
    case "proposal": {
      const proposal = data as unknown as AssistantProposal;
      const invalid = proposal.status === "invalid";
      // THIS turn's pending auto-correction notice takes the outcome (the backend renames
      // its row) — only rows after the last user row: an earlier turn's notice is the
      // ledger's, never this event's (rev-r27)
      let lastUser = messages.length - 1;
      while (lastUser >= 0 && messages[lastUser].role !== "user") lastUser--;
      const settled = messages.map((m, i) =>
        i > lastUser && m.role === "error" && m.name === PROPOSAL_AUTOCORRECT_PENDING
          ? { ...m, name: invalid ? PROPOSAL_AUTOCORRECT_FAILED : PROPOSAL_AUTOCORRECTED }
          : i > lastUser && m.role === "error" && m.name === PROPOSAL_MISSING_PENDING
            ? { ...m, name: PROPOSAL_MISSING_RECOVERED,
              missing: { base: m.missing?.base ?? null, revision: proposal.revision } }
            : m,
      );
      if (!invalid) return settled;
      // mirrors the transcript row the backend stored for this rejection
      const errs = proposal.validation_errors.map((e) => `- ${e}`).join("\n");
      return [...settled, {
        role: "error", name: PROPOSAL_REJECTED_NAME, text: errs,
        ...(proposal.validation_error_details ? { errors: proposal.validation_error_details } : {}),
      }];
    }
    case "error": {
      const code = typeof data.code === "string" ? data.code : null;
      return [
        ...messages.filter((x) => !(x.role === "assistant" && x.streaming && !x.text)),
        { role: "error", text: translateError(code, String(data.message ?? "")) },
      ];
    }
    default:
      return messages; // meta, done, preparation, heartbeat: nothing to draw
  }
}

/** The transcript after the link gave up: the streaming row is closed and the
 *  interrupted marker is shown (the ledger's own row replaces it on reload). */
export function giveUpMessages(messages: AssistantLiveMessage[]): AssistantLiveMessage[] {
  return [
    ...messages
      .filter((x) => !(x.role === "assistant" && x.streaming && !x.text))
      .map((x) => (x.streaming ? { ...x, streaming: false, reconnecting: false } : x)),
    { role: "error", name: TURN_INTERRUPTED_NAME, text: "interrupted" },
  ];
}

// ─── reconnecting stream driver ───────────────────────────────────────────

export type TurnLinkStatus = "streaming" | "reconnecting" | "reattached";

export interface TurnOutcome {
  /** `done`/`error` = the turn's terminal event was received; `aborted` = the caller's
   *  signal fired (the turn keeps running server-side); `gave_up` = no terminal event
   *  within the claim TTL after the first disconnect */
  kind: "done" | "error" | "aborted" | "gave_up";
  turn: number | null;
  lastSeq: number;
  completed: boolean;
  proposal: AssistantProposal | null;
  error: ApiError | null;
}

export interface TurnLinkOptions {
  /** the first connection: the `POST …/turns` response, or a reattach on page load */
  open: (signal: AbortSignal) => Promise<Response>;
  /** `GET …/turns/{turn}/stream?from=<seq>` */
  reattach: (turn: number, fromSeq: number, signal: AbortSignal) => Promise<Response>;
  signal: AbortSignal;
  onEvent: (evt: TurnEvent) => void;
  onStatus?: (status: TurnLinkStatus, attempt: number) => void;
  /** sleep before a retry; resolves early on abort or a host wake-up (`domWait`) */
  wait?: (ms: number, signal: AbortSignal) => Promise<void>;
  now?: () => number;
  giveUpMs?: number;
  /** known when `open` is itself a reattach (page load with `live_turn`) */
  turn?: number | null;
  fromSeq?: number;
  /** refusals of the reattach itself that end the link (default: the assistant's codes);
   *  a chat consumer passes its own — see `lib/chat-turn.ts` */
  isFinalError?: (err: unknown) => boolean;
  /** error code thrown when the stream closed before `meta` (no turn to come back to) */
  lostCode?: string;
}

const isTerminal = (evt: TurnEvent) => evt.event === "done" || evt.event === "error";

/** Refusals of the reattach itself are final; anything else (network, 5xx) is retried. */
function isFinalReattachError(err: unknown): boolean {
  return (
    err instanceof ApiError &&
    (err.code === "assistant.turn_not_found" ||
      err.code === "assistant.turn_not_yours" ||
      err.code === "assistant.conversation_not_found" ||
      isAssistantUnauthorized(err))
  );
}

/**
 * Consume a turn to its terminal event, reattaching across connection losses. The
 * first `open` failing (e.g. a 409 before the stream) throws exactly as before — the
 * caller's error handling is unchanged. Once a `meta` was seen, a close without a
 * terminal event is a reconnect, never an interruption.
 */
export async function runTurnLink(opts: TurnLinkOptions): Promise<TurnOutcome> {
  const { signal } = opts;
  const now = opts.now ?? (() => Date.now());
  const wait = opts.wait ?? defaultWait;
  const giveUpMs = opts.giveUpMs ?? TURN_RECONNECT_GIVE_UP_MS;
  const isFinal = opts.isFinalError ?? isFinalReattachError;
  let turn: number | null = opts.turn ?? null;
  let lastSeq = opts.fromSeq ?? 0;
  let completed = false;
  let proposal: AssistantProposal | null = null;
  let error: ApiError | null = null;
  let attempt = 0;
  let disconnectedAt: number | null = null;
  let first = true;
  const outcome = (kind: TurnOutcome["kind"]): TurnOutcome =>
    ({ kind, turn, lastSeq, completed, proposal, error });

  for (;;) {
    if (signal.aborted) return outcome("aborted");
    const reattaching = !first;
    let received = false;
    try {
      const res = reattaching
        ? await opts.reattach(turn as number, lastSeq, signal)
        : await opts.open(signal);
      if (reattaching) opts.onStatus?.("reattached", attempt);
      for await (const evt of sseEvents(res)) {
        if (signal.aborted) return outcome("aborted");
        received = true;
        if (evt.seq !== undefined) lastSeq = Math.max(lastSeq, evt.seq);
        const data = evt.data as Record<string, unknown>;
        if (evt.event === "meta" && typeof data.turn === "number") turn = data.turn;
        else if (evt.event === "proposal") proposal = data as unknown as AssistantProposal;
        else if (evt.event === "error") {
          const code = typeof data.code === "string" ? data.code : null;
          error = new ApiError(code ?? "assistant.turn_failed", String(data.message ?? ""), null);
        } else if (evt.event === "done") completed = true;
        opts.onEvent(evt);
        if (isTerminal(evt)) return outcome(error ? "error" : "done");
      }
      // the stream closed without a terminal event: the connection went away
    } catch (err) {
      if (signal.aborted) return outcome("aborted");
      if (first && turn === null) throw err; // the POST itself was refused: as before
      if (isFinal(err)) throw err;
      // a network failure or a transient server error: retry below
    }
    first = false;
    if (turn === null) {
      // closed before `meta`: there is no turn number to come back to
      throw new ApiError(opts.lostCode ?? "assistant.turn_lost", "the stream closed before the turn started", null);
    }
    if (received) {
      // a live stream that dropped again: the budget restarts from this disconnect
      attempt = 0;
      disconnectedAt = null;
    }
    disconnectedAt ??= now();
    if (now() - disconnectedAt > giveUpMs) return outcome("gave_up");
    opts.onStatus?.("reconnecting", attempt);
    await wait(reconnectDelay(attempt), signal);
    attempt += 1;
  }
}

function defaultWait(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done);
  });
}

/** Browser wait: the backoff sleep ends early when the page becomes visible again
 *  (phone unlocked, app foregrounded), the network comes back, or the signal aborts. */
export function domWait(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = window.setTimeout(done, ms);
    function onVisible() {
      if (document.visibilityState === "visible") done();
    }
    function done() {
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("online", done);
      signal.removeEventListener("abort", done);
      resolve();
    }
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("online", done);
    signal.addEventListener("abort", done);
  });
}

// ─── page-load reattach ───────────────────────────────────────────────────

export interface ReattachPlan {
  turn: number;
  /** the transcript to show while replaying from seq 0: the live turn's own
   *  non-user rows are dropped (the replay rebuilds them) and a streaming row appended */
  messages: AssistantLiveMessage[];
  /** true when the viewer only WATCHES another member's turn (a read-only viewer —
   *  an administrator on an unshared conversation): tail it, but it is not the
   *  viewer's turn — no composer busy state, no reattach-as-sender affordances */
  watch: boolean;
}

export interface ReattachOptions {
  /** also tail a `read_only` conversation's live turn (as `watch`); off by default so
   *  the classic console keeps reattaching to the viewer's own turns only */
  watchReadOnly?: boolean;
}

/**
 * What a page load does with `detail.live_turn`: reattach from seq 0 when the turn
 * is the viewer's (or the viewer is unknown — the server refuses another member's
 * turn with 409 anyway); with `watchReadOnly`, tail a read-only conversation's turn as
 * a watcher (the server lets a read-only viewer stream any turn); nothing otherwise.
 */
export function reattachPlan(
  detail: AssistantConversationDetail,
  viewer: string | null | undefined,
  options: ReattachOptions = {},
): ReattachPlan | null {
  const live = detail.live_turn;
  if (!live) return null;
  const foreign = Boolean(viewer && live.author && live.author !== viewer);
  const watch = foreign && Boolean(options.watchReadOnly && detail.read_only);
  if (foreign && !watch) return null;
  const rows = toLiveMessages(detail.messages).filter(
    (m) => !(m.turn === live.turn && m.role !== "user"),
  );
  return { turn: live.turn, messages: [...rows, { ...STREAMING_ROW }], watch };
}

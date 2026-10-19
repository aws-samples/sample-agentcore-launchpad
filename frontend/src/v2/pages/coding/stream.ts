// Transport policy of the coding event stream (parent design §7.4): the SSE frame
// parser and the reconnect cursor/backoff rules. The stream is a sequence of short
// GETs — `…/events?after=<last seq>` — so a dropped connection, a server-side
// `reconnect` (every ~25 min) or a hidden tab never loses an event: the next request
// resumes after the last seq the reducer applied.

export interface SseFrame {
  event: string;
  id: string | null;
  /** parsed JSON; null when the frame carried no data (or unparseable data) */
  data: unknown;
}

/** One SSE frame's text (without the blank-line terminator); null for comment-only frames. */
export function parseSseFrame(text: string): SseFrame | null {
  let event = "message";
  let id: string | null = null;
  const data: string[] = [];
  let fields = 0;
  for (const raw of text.split("\n")) {
    const line = raw.endsWith("\r") ? raw.slice(0, -1) : raw;
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    fields += 1;
    if (field === "event") event = value || "message";
    else if (field === "id") id = value;
    else if (field === "data") data.push(value);
  }
  if (fields === 0) return null;
  let parsed: unknown = null;
  if (data.length) {
    try {
      parsed = JSON.parse(data.join("\n"));
    } catch {
      parsed = null;
    }
  }
  return { event, id, data: parsed };
}

/** The frames of a `text/event-stream` body, one array per network chunk (so a
 *  history replay applies in a few renders, not one per record). */
export async function* sseFrameBatches(res: Response): AsyncGenerator<SseFrame[]> {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";
      const frames = parts.map(parseSseFrame).filter((f): f is SseFrame => f !== null);
      if (frames.length) yield frames;
    }
    buffer += decoder.decode();
    const last = buffer.trim() ? parseSseFrame(buffer) : null;
    if (last) yield [last];
  } finally {
    reader.releaseLock();
  }
}

/* ---------- reconnect policy ---------- */

/** Backoff after consecutive failures: 1 s, 2 s, 5 s, then 10 s. */
const BACKOFF_MS = [1000, 2000, 5000, 10_000];

export function nextDelay(failures: number): number {
  if (failures <= 0) return 0;
  return BACKOFF_MS[Math.min(failures, BACKOFF_MS.length) - 1];
}

/** Received records are applied at most this often. A streamed answer arrives as many
 *  tiny chunks a second, and every apply re-renders the conversation (re-parsing the
 *  streaming message's Markdown); one apply per window keeps that bounded. */
export const FLUSH_MS = 50;

/** A tab hidden longer than this stops streaming until it is visible again. */
export const HIDDEN_PAUSE_MS = 60_000;

export function shouldPause(hiddenSince: number | null, now: number): boolean {
  return hiddenSince !== null && now - hiddenSince >= HIDDEN_PAUSE_MS;
}

/** How one stream request ended. */
export type StreamEnd =
  /** server asked for a fresh request (`event: reconnect`) */
  | "reconnect"
  /** server reported a runtime error (`event: error`) */
  | "error"
  /** the body ended without a `reconnect` (a proxy closed it) */
  | "ended"
  /** fetch / read failed (network, 5xx) */
  | "failed";

/**
 * What to do after a stream request ended: the next delay and the new failure count.
 * Any event received resets the count (`gotEvents`), so a long healthy stream that
 * later drops reconnects after 1 s, not after the cap.
 */
export function afterStream(end: StreamEnd, failures: number, gotEvents: boolean): { delayMs: number; failures: number } {
  if (end === "reconnect") return { delayMs: 0, failures: 0 };
  const base = gotEvents ? 0 : failures;
  const next = base + 1;
  // a stream that delivered events and then just ended is a normal rotation: retry soon
  return { delayMs: end === "ended" && gotEvents ? 250 : nextDelay(next), failures: next };
}

/** The `after` cursor of the next request: the highest seq applied so far. */
export function resumeCursor(appliedSeq: number, frameId: string | null): number {
  const id = frameId === null ? NaN : Number(frameId);
  return Number.isFinite(id) && id > appliedSeq ? id : appliedSeq;
}

/** Error codes no retry can fix: the stream stops and the page says why. */
const TERMINAL_CODES = new Set([
  "coding.project_not_found",
  "coding.runtime_not_installed",
  // the console's auth envelope codes (routers/auth.py): a lapsed session or a
  // missing permission does not heal on retry
  "auth.required",
  "auth.permission_required",
  "auth.open_console_refused",
  "auth.forbidden",
  "workspace.forbidden",
  "workspace.not_found",
  "http.401",
  "http.403",
  "http.404",
]);

export function isTerminalStreamError(code: string | null | undefined): boolean {
  return !!code && TERMINAL_CODES.has(code);
}

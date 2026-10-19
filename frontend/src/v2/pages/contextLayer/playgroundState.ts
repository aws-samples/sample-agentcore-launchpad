import type {
  AuthRequiredEvent,
  ContextLayerDoneEvent,
  ContextLayerErrorEvent,
  ContextLayerStepEvent,
} from "../../../lib/api";
import { type AuthAsk, liveAuthAsk } from "../../../lib/user-grants";

/**
 * The Context Layer playground's turn model: one question to COA and what streamed
 * back (`step | token | done | error | auth_required`, normalized by the backend).
 * Pure, so the event handling is unit-testable without a DOM.
 */

export type TurnPhase = "streaming" | "done" | "error" | "auth" | "stopped";

export interface PlaygroundTurn {
  id: number;
  question: string;
  phase: TurnPhase;
  steps: ContextLayerStepEvent[];
  /** answer text as COA streamed it (Tier 3 tokens) */
  text: string;
  result: ContextLayerDoneEvent | null;
  error: ContextLayerErrorEvent | null;
  /** the consent ask when the member has not authorized Launchpad in COA yet */
  auth: AuthAsk | null;
}

/** A turn keeps at most this many trace steps (a deep-reasoning loop can emit many). */
export const MAX_STEPS = 100;

export const newTurn = (id: number, question: string): PlaygroundTurn => ({
  id,
  question,
  phase: "streaming",
  steps: [],
  text: "",
  result: null,
  error: null,
  auth: null,
});

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** Fold one SSE event into a turn. A finished turn ignores anything after it. */
export function applyEvent(turn: PlaygroundTurn, event: string, data: unknown): PlaygroundTurn {
  if (turn.phase !== "streaming" || !isRecord(data)) return turn;
  switch (event) {
    case "step":
      return turn.steps.length >= MAX_STEPS
        ? turn
        : { ...turn, steps: [...turn.steps, data as unknown as ContextLayerStepEvent] };
    case "token":
      return typeof data.text === "string" ? { ...turn, text: turn.text + data.text } : turn;
    case "done":
      return { ...turn, phase: "done", result: data as unknown as ContextLayerDoneEvent };
    case "error":
      return { ...turn, phase: "error", error: data as unknown as ContextLayerErrorEvent };
    case "auth_required":
      // the console grant: no agent, so the card polls `grants/{connection}/status?agent_id=`
      return { ...turn, phase: "auth", auth: liveAuthAsk(data as Partial<AuthRequiredEvent>, "") };
    default:
      return turn;
  }
}

/** The stream ended. Aborted by the member ⇒ `stopped`; otherwise a stream that
 *  never said `done` or `error` failed upstream (the network dropped it). */
export function finishTurn(turn: PlaygroundTurn, aborted: boolean): PlaygroundTurn {
  if (turn.phase !== "streaming") return turn;
  if (aborted) return { ...turn, phase: "stopped" };
  return failTurn(turn, "context_layer.upstream", "");
}

/** The request itself failed (an `ApiError` before the stream, or the network). */
export function failTurn(turn: PlaygroundTurn, code: string, message: string): PlaygroundTurn {
  if (turn.phase !== "streaming") return turn;
  return { ...turn, phase: "error", error: { code, message, status: null, coa_error: null } };
}

/** What the answer bubble shows: COA's synthesized answer, else the streamed text. */
export const answerOf = (turn: PlaygroundTurn): string => turn.result?.answer || turn.text;

/** COA's conversation id to continue: the newest answered turn's. */
export function conversationId(turns: PlaygroundTurn[]): string | null {
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const id = turns[i].result?.session_id;
    if (id) return id;
  }
  return null;
}

/** The question an auth card's retry re-sends. */
export const retryQuestion = (turn: PlaygroundTurn): string | null => (turn.question.trim() ? turn.question : null);

/** `T2`, or `T2.5` for a fractional tier; null when COA did not say. */
export function tierLabel(tier: number | null | undefined): string | null {
  if (tier == null || !Number.isFinite(tier)) return null;
  return `T${Number.isInteger(tier) ? tier : tier.toFixed(1)}`;
}

/** Confidence as a whole percentage (COA scores 0–1). */
export function confidencePct(score: number | null | undefined): number | null {
  if (score == null || !Number.isFinite(score)) return null;
  return Math.round(Math.min(1, Math.max(0, score)) * 100);
}

/** One result cell as text: `—` for null, JSON for anything structured. */
export function cellText(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

/** Numbers right-align in the rows table. */
export const numericColumn = (rows: Record<string, unknown>[], column: string): boolean =>
  rows.length > 0 && rows.every((row) => row[column] == null || typeof row[column] === "number");

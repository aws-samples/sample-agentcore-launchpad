// Pure state for the agent-detail try-chat panel (TryChat.tsx): how one SSE
// frame of `POST /api/chat/{id}` changes the turns on screen. Type-only imports
// keep it loadable in the node test environment.
import type { ChatStreamPayload } from "../../../lib/api";

export type TurnRole = "user" | "agent" | "tool" | "notice" | "error";

export interface Turn {
  role: TurnRole;
  text: string;
  /** an agent bubble still receiving deltas */
  open?: boolean;
}

/** Turns kept on screen (one user message, agent reply, tool row, … each). */
export const MAX_TURNS = 12;

/** Copy the reducer needs but cannot import (i18n lives behind lib/api). */
export interface TurnCopy {
  /** `localizedMessage(code, fallback)` */
  localize: (code: string, fallback: string) => string;
  /** fallback for an `error` frame without a message */
  sendFailed: string;
  /** a consent ask / policy denial this panel cannot render */
  needsChat: string;
}

/** Close an open agent bubble (a later delta then starts a new one). */
export function closeOpen(turns: Turn[]): Turn[] {
  const last = turns[turns.length - 1];
  if (last?.role !== "agent" || !last.open) return turns;
  return [...turns.slice(0, -1), { role: "agent", text: last.text }];
}

export function pushTurn(turns: Turn[], turn: Turn): Turn[] {
  return [...closeOpen(turns), turn].slice(-MAX_TURNS);
}

/** The turns after one stream frame. `meta` and unknown frames change nothing;
 *  the caller reads `meta.session_id` and `done` itself. */
export function applyStreamEvent(
  turns: Turn[],
  event: string,
  data: ChatStreamPayload,
  copy: TurnCopy,
): Turn[] {
  switch (event) {
    case "delta": {
      const text = data.text ?? "";
      const last = turns[turns.length - 1];
      if (last?.role === "agent" && last.open) {
        return [...turns.slice(0, -1), { ...last, text: last.text + text }];
      }
      return pushTurn(turns, { role: "agent", text, open: true });
    }
    case "tool":
      return pushTurn(turns, { role: "tool", text: data.name ?? "tool" });
    case "auth_required":
    case "policy_denied": {
      const last = turns[turns.length - 1];
      // several denials in one turn still need just the one pointer to Chat
      if (last?.role === "notice") return turns;
      return pushTurn(turns, { role: "notice", text: copy.needsChat });
    }
    case "error":
      return pushTurn(turns, {
        role: "error",
        text: copy.localize(data.code ?? "", data.message ?? copy.sendFailed),
      });
    case "done":
      return closeOpen(turns);
    default:
      return turns;
  }
}

/** Show the "thinking" placeholder: a send is in flight and no reply text is
 *  streaming into a bubble yet. */
export function awaitingReply(turns: Turn[], busy: boolean): boolean {
  if (!busy) return false;
  const last = turns[turns.length - 1];
  return !(last?.role === "agent" && last.open);
}

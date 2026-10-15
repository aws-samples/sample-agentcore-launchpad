import type { ChatAttachmentMetadata, FeedbackVerdict } from "../../../lib/api";
import type { BudgetRefusalCode } from "../../../lib/budgets";
import type { PolicyDeny } from "../../../lib/policy-deny";
import type { AuthAsk } from "../../../lib/user-grants";

export { retryPromptFor } from "../../../lib/user-grants";

export interface ChatMessage {
  kind: "user" | "agent" | "tool" | "memory" | "error" | "auth" | "policy";
  text: string;
  name?: string;
  streaming?: boolean;
  attachments?: ChatAttachmentMetadata[];
  /** kind "auth": the as_user consent ask */
  auth?: AuthAsk;
  /** kind "agent": ledger id of the persisted answer — what a thumbs verdict attaches to */
  id?: number;
  /** kind "agent": the viewer's current thumbs verdict */
  verdict?: FeedbackVerdict | null;
  /** kind "policy": a Gateway tool call a Cedar policy denied */
  policy?: PolicyDeny;
  /** kind "error": the send was refused for budget (402) — the row links to My usage */
  budget?: BudgetRefusalCode;
}

/** Append a streamed delta. An auth card asked mid-answer sits after the open
 * bubble without closing it, so the text around the card stays one bubble. */
export function appendDelta(messages: ChatMessage[], text: string, open: boolean): ChatMessage[] {
  const next = [...messages];
  let i = next.length - 1;
  while (i >= 0 && next[i].kind === "auth") i -= 1;
  const bubble = next[i];
  if (open && bubble?.kind === "agent" && bubble.streaming) {
    next[i] = { ...bubble, text: bubble.text + text };
  } else {
    next.push({ kind: "agent", text, streaming: true });
  }
  return next;
}

/** The console stream's `saved {message_id}` event: the open answer was just
 * persisted. It arrives *before* the event that closes the bubble (tool / error /
 * done), so the answer is still the last agent bubble — past any auth cards asked
 * mid-answer. Stamps its id there; anything else (no open answer, or one already
 * stamped) leaves the thread unchanged. */
export function stampSaved(messages: ChatMessage[], messageId: number): ChatMessage[] {
  let i = messages.length - 1;
  while (i >= 0 && messages[i].kind === "auth") i -= 1;
  const bubble = messages[i];
  if (bubble?.kind !== "agent" || bubble.id != null || !Number.isInteger(messageId)) return messages;
  const next = [...messages];
  next[i] = { ...bubble, id: messageId };
  return next;
}

/** Clicking a thumb: the same thumb again withdraws (null), the other one switches. */
export function nextVerdict(current: FeedbackVerdict | null | undefined, clicked: FeedbackVerdict): FeedbackVerdict | null {
  return current === clicked ? null : clicked;
}

/** Set the verdict on the agent answer with ledger id `messageId` (no-op when absent,
 * e.g. after the thread switched sessions). */
export function setVerdict(messages: ChatMessage[], messageId: number, verdict: FeedbackVerdict | null): ChatMessage[] {
  const i = messages.findIndex((m) => m.kind === "agent" && m.id === messageId);
  if (i < 0 || messages[i].verdict === verdict) return messages;
  const next = [...messages];
  next[i] = { ...messages[i], verdict };
  return next;
}

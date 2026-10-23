import type {
  ChatAttachmentMetadata,
  ChatHistoryMessage,
  ChatReplyAttachment,
  FeedbackVerdict,
} from "../../../lib/api";
import type { BudgetRefusalCode } from "../../../lib/budgets";
import { type PolicyDeny, restoredPolicyDeny } from "../../../lib/policy-deny";
import { type AuthAsk, restoredAuthAsk } from "../../../lib/user-grants";

export { retryPromptFor } from "../../../lib/user-grants";

export interface ChatMessage {
  kind: "user" | "agent" | "tool" | "memory" | "error" | "auth" | "policy";
  text: string;
  name?: string;
  streaming?: boolean;
  attachments?: ChatAttachmentMetadata[];
  /** kind "agent": the images the agent answered with (metadata; bytes fetched by id) */
  replyAttachments?: ChatReplyAttachment[];
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

/** One persisted history row as the thread item it was rendered as — an agent
 * answer keeps its reply images (metadata; the bytes are fetched again by id). */
export function restoredMessage(r: ChatHistoryMessage, agentId: string): ChatMessage {
  if (r.role === "user") {
    return { kind: "user", text: r.text, attachments: r.attachments as ChatAttachmentMetadata[] | undefined };
  }
  if (r.role === "agent") {
    return {
      kind: "agent",
      text: r.text,
      id: r.id,
      verdict: r.verdict ?? null,
      replyAttachments: r.attachments?.length ? (r.attachments as ChatReplyAttachment[]) : undefined,
    };
  }
  if (r.role === "tool") return { kind: "tool", text: r.name ?? "tool", name: r.name ?? "tool" };
  if (r.role === "auth") {
    // the URL is single-use and never persisted: a restored card retries
    return { kind: "auth", text: r.text, name: r.name ?? "", auth: restoredAuthAsk(r, agentId) };
  }
  if (r.role === "policy") {
    return { kind: "policy", text: r.text, name: r.name ?? "", policy: restoredPolicyDeny(r) };
  }
  return { kind: "error", text: r.text };
}

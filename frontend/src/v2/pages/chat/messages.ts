import type { ChatAttachmentMetadata } from "../../../lib/api";
import type { PolicyDeny } from "../../../lib/policy-deny";
import type { AuthAsk } from "../../../lib/user-grants";

export { retryPromptFor } from "../../../lib/user-grants";
export { markTruncated } from "../../../lib/chat";
// the one delta reducer of both Chat pages (N-ARCH-obs15 v2: the page-load replay must
// build its rows exactly as a just-sent turn does)
export { appendDelta } from "../../../lib/chat-turn";

export interface ChatMessage {
  kind: "user" | "agent" | "tool" | "memory" | "error" | "auth" | "policy";
  text: string;
  name?: string;
  streaming?: boolean;
  /** kind "agent", while streaming: the connection dropped and the link is reattaching
   *  to the turn that keeps running server-side (N-ARCH-obs15 v2) */
  reconnecting?: boolean;
  attachments?: ChatAttachmentMetadata[];
  /** kind "auth": the as_user consent ask */
  auth?: AuthAsk;
  /** kind "agent": the runtime closed this answer at the model's token limit (N-SCN-bug4) */
  truncated?: boolean;
  /** kind "policy": a call the self-hosted Cedar policy denied (D20) */
  deny?: PolicyDeny;
  /** kind "tool" (N16-obs85): the runtime's tool-use id — what `tool_input` / `tool_result`
   *  frames are matched on */
  toolId?: string | null;
  /** kind "tool": the call's arguments (≤ 512 chars) and the bounded result summary */
  toolInput?: string;
  toolResult?: string;
  toolStatus?: string;
}

/** A `tool_input` / `tool_result` frame (N16-obs85), as the page's reducer sees it. */
export interface ToolDetailFrame {
  id?: string | null;
  name?: string;
  input?: string;
  summary?: string;
  status?: string;
}

/**
 * Complete the tool row a `tool_input` / `tool_result` frame belongs to: the newest tool
 * row with the frame's id, else (no id on either side) the newest tool row of that name,
 * else the newest tool row. A frame nothing matches is dropped — a row is never invented
 * (history does the same: `update_tool_message`).
 */
export function applyToolDetail(messages: ChatMessage[], frame: ToolDetailFrame): ChatMessage[] {
  let index = -1;
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i];
    if (msg.kind !== "tool") continue;
    if (frame.id) {
      if (msg.toolId === frame.id) { index = i; break; }
      continue;
    }
    if (frame.name && msg.name !== frame.name) continue;
    index = i;
    break;
  }
  if (index < 0) return messages;
  const current = messages[index];
  const next: ChatMessage = { ...current };
  if (frame.input !== undefined) next.toolInput = frame.input;
  if (frame.summary !== undefined) next.toolResult = frame.summary;
  if (frame.status) next.toolStatus = frame.status;
  return messages.map((msg, i) => (i === index ? next : msg));
}

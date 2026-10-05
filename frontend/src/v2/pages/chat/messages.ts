import type { ChatAttachmentMetadata } from "../../../lib/api";
import type { AuthAsk } from "../../../lib/user-grants";

export { retryPromptFor } from "../../../lib/user-grants";

export interface ChatMessage {
  kind: "user" | "agent" | "tool" | "memory" | "error" | "auth";
  text: string;
  name?: string;
  streaming?: boolean;
  attachments?: ChatAttachmentMetadata[];
  /** kind "auth": the as_user consent ask */
  auth?: AuthAsk;
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

import type { ChatReplyAttachment } from "../../lib/api";

/** The thread items both consoles keep; only what a reply image touches. */
interface ThreadItem {
  kind: string;
  text: string;
  streaming?: boolean;
  replyAttachments?: ChatReplyAttachment[];
}

/** Append a streamed reply image to the open answer bubble — or open one, exactly
 * where the backend persists it (the answer the next tool / error / done closes).
 * Auth cards asked mid-answer sit after the open bubble without closing it. */
export function appendReplyAttachment<M extends ThreadItem>(
  messages: M[],
  attachment: ChatReplyAttachment,
  open: boolean,
): M[] {
  const next = [...messages];
  let i = next.length - 1;
  while (i >= 0 && next[i].kind === "auth") i -= 1;
  const bubble = next[i];
  if (open && bubble?.kind === "agent" && bubble.streaming) {
    next[i] = { ...bubble, replyAttachments: [...(bubble.replyAttachments ?? []), attachment] };
  } else {
    next.push({ kind: "agent", text: "", streaming: true, replyAttachments: [attachment] } as M);
  }
  return next;
}

/** A stream `attachment` payload as reply-image metadata; null when malformed. */
export function replyAttachmentOf(payload: {
  id?: string;
  name?: string;
  media_type?: string;
  size_bytes?: number;
  sha256?: string;
  status?: string;
  reason_code?: string;
}): ChatReplyAttachment | null {
  if (payload.status !== "stored" && payload.status !== "dropped") return null;
  if (payload.status === "stored" && !payload.id) return null;
  return {
    name: payload.name ?? "",
    status: payload.status,
    id: payload.id,
    media_type: payload.media_type,
    size_bytes: payload.size_bytes,
    sha256: payload.sha256,
    reason_code: payload.reason_code,
  };
}

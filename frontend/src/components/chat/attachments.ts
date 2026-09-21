import type {
  AttachmentCapability,
  ChatAttachment,
  ChatAttachmentMetadata,
} from "../../lib/api";

export interface PendingAttachment {
  id: string;
  file: File;
  mediaType: string;
}

const EXTENSION_TYPES: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  pdf: "application/pdf",
  txt: "text/plain",
  md: "text/markdown",
  csv: "text/csv",
  json: "application/json",
  yaml: "text/yaml",
  yml: "text/yaml",
  xml: "application/xml",
  log: "text/plain",
};

export function attachmentMediaType(file: File): string {
  // Browsers often leave text/code MIME empty or report octet-stream.
  const declared = file.type.toLowerCase().split(";")[0];
  return declared && declared !== "application/octet-stream"
    ? declared
    : EXTENSION_TYPES[file.name.split(".").pop()?.toLowerCase() ?? ""] ?? declared;
}

export function formatAttachmentSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Number((bytes / 1024).toFixed(1))} KiB`;
  return `${Number((bytes / (1024 * 1024)).toFixed(1))} MiB`;
}

export type AttachmentValidationError =
  | { key: "unavailable" | "empty" | "imageUnsupported" | "format"; name?: string }
  | { key: "count"; count: number }
  | { key: "fileSize" | "totalSize"; size: string; name?: string };

/** Fast UX checks only; the backend validates actual bytes and extraction limits. */
export function validateAttachments(
  files: File[],
  capability: AttachmentCapability | undefined,
): AttachmentValidationError | null {
  if (!capability) return { key: "unavailable" };
  if (files.length > capability.max_files) return { key: "count", count: capability.max_files };
  let total = 0;
  for (const file of files) {
    const mediaType = attachmentMediaType(file);
    if (!file.size) return { key: "empty", name: file.name };
    if (file.size > capability.max_file_bytes) {
      return { key: "fileSize", name: file.name, size: formatAttachmentSize(capability.max_file_bytes) };
    }
    if (mediaType.startsWith("image/") && !capability.images) {
      return { key: "imageUnsupported", name: file.name };
    }
    const kindAllowed = mediaType.startsWith("image/")
      ? capability.images
      : mediaType === "application/pdf"
        ? capability.pdf !== "unsupported"
        : capability.text;
    const accepted = capability.accept.some((pattern) => {
      const accept = pattern.toLowerCase();
      if (accept.startsWith(".")) return file.name.toLowerCase().endsWith(accept);
      if (accept.endsWith("/*")) return mediaType.startsWith(accept.slice(0, -1));
      return mediaType === accept;
    });
    if (!kindAllowed || !accepted) return { key: "format", name: file.name };
    total += file.size;
  }
  return total > capability.max_total_bytes
    ? { key: "totalSize", size: formatAttachmentSize(capability.max_total_bytes) }
    : null;
}

export function attachmentMetadata(
  attachment: PendingAttachment,
  capability: AttachmentCapability,
): ChatAttachmentMetadata {
  return {
    name: attachment.file.name,
    media_type: attachment.mediaType,
    size: attachment.file.size,
    delivery: attachment.mediaType === "application/pdf"
      ? capability.pdf === "text" ? "pdf_text" : "native"
      : attachment.mediaType.startsWith("image/") ? "native" : "text",
  };
}

/** Read only at send time; neither encoded bytes nor data URLs enter the transcript. */
export function encodeAttachment(attachment: PendingAttachment): Promise<ChatAttachment> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("attachment_read_failed"));
    reader.onabort = () => reject(new Error("attachment_read_failed"));
    reader.onload = () => {
      if (typeof reader.result !== "string" || !reader.result.includes(",")) {
        reject(new Error("attachment_read_failed"));
        return;
      }
      resolve({
        name: attachment.file.name,
        media_type: attachment.mediaType,
        data: reader.result.slice(reader.result.indexOf(",") + 1),
      });
    };
    reader.readAsDataURL(attachment.file);
  });
}

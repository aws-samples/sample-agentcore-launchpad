import type { ChipTone } from "../../components";
import { localizedMessage } from "../../lib/api";
import { resourceState } from "../../lib/knowledgeBases";

export { extractConflictAgents, formatBytes } from "../../lib/knowledgeBases";

/** Tone for data-source / ingestion-job / document statuses (raw AWS enums). */
export function resourceTone(status: string): ChipTone {
  return resourceState(status);
}

/** Pull a human message out of an error body ({code,message,detail} envelope or
 *  a FastAPI {detail} shape). A code the console has copy for (`apiErrors.*`,
 *  e.g. `aws.access_denied` for an unknown id) wins over the backend's English. */
export function kbErrorMessage(body: unknown, status: number): string {
  const b = (body ?? {}) as { code?: unknown; message?: unknown; detail?: unknown };
  const fallback = typeof b.message === "string" ? b.message : "";
  if (typeof b.code === "string") {
    const localized = localizedMessage(b.code, fallback);
    if (localized) return localized;
  }
  if (typeof b.message === "string") return b.message;
  if (typeof b.detail === "string") return b.detail;
  if (b.detail && typeof b.detail === "object") {
    const d = b.detail as { message?: unknown };
    if (typeof d.message === "string") return d.message;
  }
  return `HTTP ${status}`;
}

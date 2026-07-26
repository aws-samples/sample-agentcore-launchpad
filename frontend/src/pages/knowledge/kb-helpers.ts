import type { ChipTone } from "../../components";

/** Tone for data-source / ingestion-job / document statuses (raw AWS enums). */
export function resourceTone(status: string): ChipTone {
  const s = status.toUpperCase();
  if (["AVAILABLE", "COMPLETE", "COMPLETED", "READY", "ACTIVE", "INDEXED"].includes(s)) {
    return "good";
  }
  if (["FAILED", "DELETE_FAILED", "NOT_FOUND"].includes(s)) return "crit";
  if (
    ["CREATING", "IN_PROGRESS", "STARTING", "SYNCING", "STOPPING", "PENDING",
     "INDEXING", "PARTIALLY_INDEXED", "DELETING", "DELETE_IN_PROGRESS"].includes(s)
  ) {
    return "warn";
  }
  return "muted";
}

/** 361727 → "353.2 KB" — document sizes in the data-source panel. */
export function formatBytes(size: number | null | undefined): string {
  if (size == null) return "—";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

/** Pull a human message out of an error body ({code,message,detail} envelope or
 *  a FastAPI {detail} shape). */
export function kbErrorMessage(body: unknown, status: number): string {
  const b = (body ?? {}) as { message?: unknown; detail?: unknown };
  if (typeof b.message === "string") return b.message;
  if (typeof b.detail === "string") return b.detail;
  if (b.detail && typeof b.detail === "object") {
    const d = b.detail as { message?: unknown };
    if (typeof d.message === "string") return d.message;
  }
  return `HTTP ${status}`;
}

/** Agent names carried by a DELETE 409 (attached & not forced). Tolerant of
 *  either a top-level `agents` or a nested `detail.agents`. */
export function extractConflictAgents(body: unknown): string[] {
  const b = (body ?? {}) as { agents?: unknown; detail?: { agents?: unknown } };
  const raw = Array.isArray(b.agents)
    ? b.agents
    : Array.isArray(b.detail?.agents)
      ? b.detail?.agents
      : [];
  return (raw as unknown[]).filter((x): x is string => typeof x === "string");
}

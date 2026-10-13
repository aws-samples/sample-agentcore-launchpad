// Pure state of a knowledge base created from inside the agent wizard: the record,
// its phase (derived from the KB detail), the transitions the hook applies to API
// results, and the deploy gate. No React, no API calls, so it is unit-testable.
import type { KnowledgeBaseDetail } from "../../../lib/knowledgeBases";
import type { TagTone } from "../../ui";

/** A knowledge base created from inside the wizard (upload mode). */
export interface InlineKb {
  kb_id: string;
  name: string;
  /** kept so a failed upload can be retried without re-picking */
  files: File[];
  uploaded: boolean;
  uploadError: string | null;
  detail: KnowledgeBaseDetail | null;
}

export type InlineKbPhase =
  | "creating"
  | "sourcePending"
  | "starting"
  | "indexing"
  | "ready"
  | "ingestFailed"
  | "failed"
  | "unknown";

export const PHASE_TONE: Record<InlineKbPhase, TagTone> = {
  creating: "blue",
  sourcePending: "blue",
  starting: "blue",
  indexing: "blue",
  ready: "green",
  ingestFailed: "red",
  failed: "red",
  unknown: "gray",
};

/** How often the wizard re-reads the KB while its phase still changes on its own. */
export const POLL_MS = 5000;

/** Where a KB is on its way from "just created" to "answering". */
export function inlineKbPhase(d: KnowledgeBaseDetail | null): InlineKbPhase {
  if (!d) return "unknown";
  const status = String(d.status).toUpperCase();
  if (status === "CREATING") return "creating";
  if (status === "FAILED" || status === "DELETING") return "failed";
  const source = d.data_sources[0];
  if (!source || source.status.toUpperCase() !== "AVAILABLE") return "sourcePending";
  const job = (source.ingestion_jobs ?? [])[0]; // newest first
  if (!job) return "starting";
  const js = job.status.toUpperCase();
  if (js === "COMPLETE") return "ready";
  if (js === "FAILED") return "ingestFailed";
  return "indexing";
}

/** The phase shown on the record's status tag. Until the files are uploaded the
 *  KB is not followed, so its state is "unknown" (checking). */
export const displayPhase = (inline: InlineKb): InlineKbPhase => (inline.uploaded ? inlineKbPhase(inline.detail) : "unknown");

/** The i18n key of the record's status tag. */
export const phaseLabelKey = (inline: InlineKb) => `v2.agents.wizard.inlineKb.state.${displayPhase(inline)}`;

/** Whether the phase still changes on its own (worth polling again). */
export const phaseInFlight = (p: InlineKbPhase) =>
  p === "creating" || p === "sourcePending" || p === "starting" || p === "indexing" || p === "unknown";

/* ── transitions ─────────────────────────────────────────────────────── */

/** POST /api/knowledge-bases answered (202, usually still CREATING). */
export const created = (name: string, files: File[], detail: KnowledgeBaseDetail): InlineKb => ({
  kb_id: detail.kb_id,
  name,
  files,
  uploaded: false,
  uploadError: null,
  detail,
});

export const uploadSucceeded = (rec: InlineKb): InlineKb => ({ ...rec, uploaded: true, uploadError: null });

export const uploadFailed = (rec: InlineKb, msg: string): InlineKb => ({ ...rec, uploadError: msg });

/** A poll read: only the record it was issued for takes the fresh detail (a
 *  discarded or replaced record is left as it is). */
export const refreshed = (prev: InlineKb | null, kbId: string, detail: KnowledgeBaseDetail): InlineKb | null =>
  prev && prev.kb_id === kbId ? { ...prev, detail } : prev;

/** The data source whose first ingestion the wizard should start now, or null.
 *  Mirrors the KB detail page's auto-sync: once, when the source is AVAILABLE and
 *  has no ingestion job yet. */
export function syncTarget(detail: KnowledgeBaseDetail, synced: ReadonlySet<string>): string | null {
  if (inlineKbPhase(detail) !== "starting") return null;
  const source = detail.data_sources[0];
  return source && !synced.has(source.ds_id) ? source.ds_id : null;
}

/** Mount / unmount the KB on the wizard form (same list an existing-KB pick edits). */
export const mountKb = (selected: string[], id: string) => (selected.includes(id) ? selected : [...selected, id]);
export const unmountKb = (selected: string[], id: string) => selected.filter((k) => k !== id);

/* ── the deploy gate ─────────────────────────────────────────────────── */

export type InlineKbBlock = "busy" | "waiting" | "failed" | null;

/**
 * Why deploy must wait for the inline KB, or null.
 * - `busy`: the create / upload calls are still running;
 * - `waiting`: the mounted KB is not ACTIVE yet (its gateway Retrieve target, or the
 *   generated code's KB reference, needs a KB that exists);
 * - `failed`: the mounted KB failed to create.
 * Ingestion never blocks: deploy does not wait for it, the agent answers from the
 * KB once it finishes. A KB that is not mounted (upload failed, or unticked) never
 * blocks either.
 */
export function submitBlock(inline: InlineKb | null, selected: string[], busy: boolean): InlineKbBlock {
  if (busy) return "busy";
  if (!inline || !inline.uploaded || !selected.includes(inline.kb_id)) return null;
  if (inlineKbPhase(inline.detail) === "failed") return "failed";
  return String(inline.detail?.status ?? "CREATING").toUpperCase() === "ACTIVE" ? null : "waiting";
}

export const blocksSubmit = (inline: InlineKb | null, selected: string[], busy: boolean) =>
  submitBlock(inline, selected, busy) !== null;

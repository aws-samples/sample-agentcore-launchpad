/**
 * Managed knowledge bases — wire types and pure rules shared by the classic
 * `/knowledge-bases` page and the native V2 page (`v2/pages/knowledge/`).
 */
import { timeMs } from "./timestamps";

export type KBStatus = "CREATING" | "ACTIVE" | "FAILED" | "DELETING";

export interface KnowledgeBaseSummary {
  kb_id: string;
  name: string;
  description: string;
  status: KBStatus | string;
  updated_at: string | null;
  data_source_count: number;
  attached_agents: string[];
  /** Every mounting agent, including those hidden by workspace isolation (whose
   *  names `attached_agents` omits) — so "in use" stays truthful. */
  attached_agent_count?: number;
  /** `users.id` of the creating account (the ownership key); null for the config
   *  admin / auth-disabled operator, system-created and legacy rows. */
  created_by?: string | null;
  /** Creator's username at creation time (display only); null when unknown —
   *  legacy rows and resources created outside the console. */
  created_by_name?: string | null;
}

/** The extractor's job counts. `prefix_empty` is present (and true) only when the job's
 *  listing held no original — nothing under the prefix, or `_text/` sidecars only
 *  (N17-obs204); the table names that instead of showing three zeros. */
export interface IngestionJobStatistics {
  numberOfDocumentsScanned?: number;
  numberOfNewDocumentsIndexed?: number;
  numberOfDocumentsFailed?: number;
  /** Same ETag + size as the last INDEXED record — skipped (N17-obs246). */
  numberOfDocumentsUnchanged?: number;
  /** Swept: no longer under the prefix (N17-obs246); present only when > 0. */
  numberOfDocumentsDeleted?: number;
  prefix_empty?: boolean;
  [key: string]: number | boolean | undefined;
}

export interface IngestionJob {
  job_id: string;
  status: string;
  started_at?: string | null;
  updated_at?: string | null;
  statistics?: IngestionJobStatistics;
  failure_reasons?: string[];
}

/** The ingestion sync a successful upload started (N17-obs204): `coalesced` when a job of
 *  the source was already running and no second one was enqueued; `covers_upload: false`
 *  when that running job started before this upload and may not list its files (the next
 *  upload or a manual sync does). */
export interface UploadSyncJob {
  job_id: string;
  status: string;
  coalesced: boolean;
  covers_upload: boolean;
}

/** `POST /api/knowledge-bases/{kb_id}/files` — the files are stored whenever this
 *  answers; `job` is null when the server could not start the sync (sync by hand).
 *  `data_source` is present only when the KB had no upload-mode source and this upload
 *  created one (N17-obs242) — the files sit under that new source's prefix. */
export interface UploadFilesResult {
  keys: string[];
  job: UploadSyncJob | null;
  data_source?: DataSource;
}

/** The toast an upload earns from the sync it started (N17-obs204): fresh, or coalesced
 *  onto a job that began after the upload → indexing is under way; coalesced onto an
 *  earlier job, or no job → the files are stored but need a manual sync. */
export function uploadOutcomeKey(job: UploadSyncJob | null | undefined): string {
  if (!job) return "knowledge.detail.sources.uploadedSyncUnavailable";
  if (!job.coalesced) return "knowledge.detail.sources.uploadedIndexing";
  return job.covers_upload
    ? "knowledge.detail.sources.uploadedSyncRunning"
    : "knowledge.detail.sources.uploadedSyncManual";
}

/** A finished job that found nothing to ingest — COMPLETE with `prefix_empty`, or with 0
 *  documents scanned (N17-obs244: seven such COMPLETE rows read as "ingestion works" while
 *  the KB held no file at all). */
export function jobFoundNoFiles(job: IngestionJob | null | undefined): boolean {
  if (!job || !["COMPLETE", "COMPLETED"].includes(String(job.status).toUpperCase())) return false;
  const stats = job.statistics;
  if (!stats) return false;
  return stats.prefix_empty === true || stats.numberOfDocumentsScanned === 0;
}

/** Whether the KB is known to hold no file in any data source (N17-obs244) — no source at
 *  all, or every source either lists no object (`has_objects: false`) or, when the server
 *  could not tell, its latest job found nothing. A source with `has_objects: true` holds
 *  files whatever its last job said. */
export function kbHoldsNoFiles(sources: DataSource[]): boolean {
  return sources.every(
    (ds) => ds.has_objects === false || (ds.has_objects !== true && jobFoundNoFiles(ds.ingestion_jobs?.[0])),
  );
}

/** Whether the KB never had a data source: the self-hosted backend writes the row with its
 *  source in one INSERT (`created_at === updated_at`), and every later source change —
 *  removing one, an upload creating one — bumps `updated_at`. A KB with 0 sources and an
 *  untouched row is still in its creation window (the case the repair button is for); one
 *  whose row changed lost its last source on purpose (N17-obs243). */
export function kbNeverChanged(kb: Pick<KnowledgeBaseDetail, "created_at" | "updated_at">): boolean {
  if (!kb.updated_at || !kb.created_at) return true;
  return timeMs(kb.updated_at) === timeMs(kb.created_at);
}

/** The numeric statistics of a job, `prefix_empty` set aside — the known keys first, in
 *  `STAT_ORDER` (scanned · newly indexed · unchanged · failed · removed), then any other
 *  numeric key in the order the server sent it (N17-obs271). */
export function numericStats(stats: IngestionJobStatistics): Array<[string, number]> {
  const entries = Object.entries(stats).filter((e): e is [string, number] => typeof e[1] === "number");
  const rank = (k: string) => {
    const i = STAT_ORDER.indexOf(k);
    return i < 0 ? STAT_ORDER.length : i;
  };
  return entries
    .map((e, i) => ({ e, i }))
    .sort((a, b) => rank(a.e[0]) - rank(b.e[0]) || a.i - b.i)
    .map(({ e }) => e);
}

/** The ingestion-job statistic keys the console labels itself (N17-obs271), in display
 *  order; their i18n keys under `v2.knowledge`. Any other key falls back to `humanizeStat`. */
export const STAT_ORDER = [
  "numberOfDocumentsScanned",
  "numberOfNewDocumentsIndexed",
  "numberOfDocumentsUnchanged",
  "numberOfDocumentsFailed",
  "numberOfDocumentsDeleted",
];
export const STAT_LABEL_KEYS: Record<string, string> = {
  numberOfDocumentsScanned: "v2.knowledge.statScanned",
  numberOfNewDocumentsIndexed: "v2.knowledge.statIndexed",
  numberOfDocumentsUnchanged: "v2.knowledge.statUnchanged",
  numberOfDocumentsFailed: "v2.knowledge.statFailed",
  numberOfDocumentsDeleted: "v2.knowledge.statRemoved",
};
/** Hover explanations for the two statistics whose meaning is not obvious. */
export const STAT_TITLE_KEYS: Record<string, string> = {
  numberOfDocumentsUnchanged: "v2.knowledge.statUnchangedTitle",
  numberOfDocumentsDeleted: "v2.knowledge.statRemovedTitle",
};

/** The label of one statistic key: the console's own copy for a known key, else the
 *  humanized camel case (an AWS-shaped key the console has no copy for). */
export function statLabel(t: (key: string) => string, key: string): string {
  const i18nKey = STAT_LABEL_KEYS[key];
  return i18nKey ? t(i18nKey) : humanizeStat(key);
}

/** One line for a job's statistics, e.g. 「扫描 4 · 新索引 1 · 未变化 3 · 失败 0」 /
 *  "Scanned 4 · Newly indexed 1 · Unchanged 3 · Failed 0"; "" when there is none. */
export function jobStatsSummary(t: (key: string) => string, stats: IngestionJobStatistics | undefined): string {
  if (!stats) return "";
  return numericStats(stats)
    .map(([k, v]) => `${statLabel(t, k)} ${v}`)
    .join(" · ");
}

export interface DataSource {
  ds_id: string;
  name: string;
  /** `upload` (the KB's own artifacts-bucket prefix) or `existing` (a location the
   *  member named). Additive — older details may omit it. */
  mode?: "upload" | "existing" | string;
  status: string;
  bucket: string | null;
  prefix?: string | null;
  failure_reasons?: string[];
  ingestion_jobs?: IngestionJob[];
  /** True when the detail could not refresh this source's jobs from OpenSearch and
   *  `ingestion_jobs` is the last cached list (or empty) — N-SCN-obs11. Additive. */
  jobs_degraded?: boolean;
  /** Whether at least one object other than a `_text/` sidecar sits under the prefix;
   *  null when the server could not list it. The automatic first sync needs `true`
   *  (N17-obs204). Additive. */
  has_objects?: boolean | null;
}

export interface KnowledgeBaseDetail {
  kb_id: string;
  name: string;
  description: string;
  status: KBStatus | string;
  arn?: string | null;
  created_at?: string | null;
  updated_at: string | null;
  failure_reasons?: string[];
  data_sources: DataSource[];
  /** Any data source's `jobs_degraded` (N-SCN-obs11). Additive. */
  jobs_degraded?: boolean;
  attached_agents: string[];
  /** Every mounting agent, including those hidden by workspace isolation (whose
   *  names `attached_agents` omits) — so "in use" stays truthful. */
  attached_agent_count?: number;
  /** `users.id` of the creating account (the ownership key); null for the config
   *  admin / auth-disabled operator, system-created and legacy rows. */
  created_by?: string | null;
  /** Creator's username at creation time (display only); null when unknown —
   *  legacy rows and resources created outside the console. */
  created_by_name?: string | null;
}

/** `DELETE /api/knowledge-bases/{kb_id}` (WB-008). The row is gone whenever `deleted`
 *  is true; `cleanup` reports the three best-effort storage steps and `cleanup_warnings`
 *  names the ones still `pending` — a `kb_cleanup` job (`cleanup_job_id`, readable on
 *  `GET /api/jobs/{id}`) retries them in the background. All three fields are additive. */
export interface KnowledgeBaseDeleteResult {
  kb_id: string;
  deleted: boolean;
  cleanup?: Record<"s3" | "index" | "docs", "ok" | "pending">;
  cleanup_warnings?: string[];
  cleanup_job_id?: string | null;
}

/** True when the delete succeeded but some storage cleanup is still being retried. */
export function hasPendingCleanup(out: unknown): boolean {
  const o = out as Partial<KnowledgeBaseDeleteResult> | null | undefined;
  return Array.isArray(o?.cleanup_warnings) && o.cleanup_warnings.length > 0;
}

export interface QueryResultItem {
  text: string;
  score: number | null;
  location_uri: string | null;
  metadata: Record<string, unknown>;
}

/** One document of a data source (`ListKnowledgeBaseDocuments` joined with S3
 *  for size / upload time). */
export interface KBDocument {
  name: string;
  uri: string;
  status: string;
  status_reason?: string | null;
  indexed_at?: string | null;
  size_bytes?: number | null;
  uploaded_at?: string | null;
}

/** One token page of documents — `next_token` null on the last page. */
export interface KBDocumentPage {
  documents: KBDocument[];
  next_token: string | null;
}

/** Source of a KB / data source: files uploaded to the artifacts bucket, or an
 *  existing S3 location the KB service role is granted read access to. */
export type KBSourceBody = { mode: "upload" } | { mode: "existing"; bucket: string; prefix?: string };

export type KBSourceMode = KBSourceBody["mode"];

/** Mirrors the backend rule — starts alphanumeric, then letters/digits/-/_ (no
 *  spaces; the AWS KB name constraint), 100 characters at most. */
export const KB_NAME_RE = /^[a-zA-Z0-9][a-zA-Z0-9\-_]{0,99}$/;
export const KB_DESCRIPTION_MAX = 1000;
export const KB_QUERY_MAX_RESULTS = 100;

/** Document types the ingestion Lambda indexes — mirrors
 *  `backend/lambdas/memory_extractor/handler.py::NATIVE_TEXT_EXT` (indexed as UTF-8 text)
 *  plus `.pdf`, whose text the console upload extracts into a `_text/` sidecar
 *  (`knowledge_selfhosted.upload_files`). Anything else is marked UNSUPPORTED after
 *  ingestion, so the picker refuses it up front (N17-obs239). */
export const KB_SUPPORTED_EXT = [".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm", ".pdf"] as const;
/** The `accept` attribute of the file input. */
export const KB_FILE_ACCEPT = KB_SUPPORTED_EXT.join(",");
/** Mirrors the Lambda's `MAX_DOC_BYTES`: text over this is skipped ("document larger than 2 MB"). */
export const KB_MAX_DOC_BYTES = 2_000_000;
/** Mirrors the Lambda's `MAX_CHUNKS`: a longer document is truncated after this many chunks. */
export const KB_MAX_CHUNKS = 400;

/** Case-insensitive extension match against `KB_SUPPORTED_EXT` (`A.PDF` passes). */
export function kbFileSupported(name: string): boolean {
  const lower = name.toLowerCase();
  return KB_SUPPORTED_EXT.some((ext) => lower.endsWith(ext));
}

/** The names of a pick the picker must refuse (unsupported extension). */
export function unsupportedFileNames(files: File[]): string[] {
  return files.filter((f) => !kbFileSupported(f.name)).map((f) => f.name);
}

/** Health class of a data-source / ingestion-job / document status (raw AWS enums). */
export type ResourceState = "good" | "crit" | "warn" | "muted";

export function resourceState(status: string): ResourceState {
  const s = status.toUpperCase();
  if (["AVAILABLE", "COMPLETE", "COMPLETED", "READY", "ACTIVE", "INDEXED"].includes(s)) {
    return "good";
  }
  if (["FAILED", "DELETE_FAILED", "NOT_FOUND"].includes(s)) return "crit";
  if (
    ["CREATING", "IN_PROGRESS", "STARTING", "SYNCING", "STOPPING", "PENDING",
     "INDEXING", "PARTIALLY_INDEXED", "DELETING", "DELETE_IN_PROGRESS", "UPDATING"].includes(s)
  ) {
    return "warn";
  }
  return "muted";
}

/** A KB is "in flight" (worth polling) while it is provisioning, any data source
 *  is being created/deleted, or any ingestion job is still running. */
export function kbInFlight(d: KnowledgeBaseDetail): boolean {
  if (["CREATING", "DELETING", "UPDATING"].includes(String(d.status).toUpperCase())) return true;
  for (const ds of d.data_sources) {
    if (["CREATING", "DELETING"].includes(ds.status.toUpperCase())) return true;
    for (const j of ds.ingestion_jobs ?? []) {
      if (jobRunning(j, true)) return true;
    }
  }
  return false;
}

/** An ingestion job still running (`stopping` counts only for polling). */
export function jobRunning(job: IngestionJob, includeStopping = false): boolean {
  const s = job.status.toUpperCase();
  return s === "IN_PROGRESS" || s === "STARTING" || (includeStopping && s === "STOPPING");
}

/** "numberOfDocumentsScanned" → "Documents Scanned" (AWS stat keys, shown raw). */
export function humanizeStat(key: string): string {
  return key.replace(/^numberOf/, "").replace(/([A-Z])/g, " $1").trim();
}

/** 361727 → "353.2 KB" — document sizes. */
export function formatBytes(size: number | null | undefined): string {
  if (size == null) return "—";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

/** Append picked files, de-duping by name+size so the same file isn't added twice. */
export function mergeFiles(current: File[], picked: File[]): File[] {
  const seen = new Set(current.map((f) => `${f.name}:${f.size}`));
  const merged = [...current];
  for (const f of picked) {
    const key = `${f.name}:${f.size}`;
    if (!seen.has(key)) {
      seen.add(key);
      merged.push(f);
    }
  }
  return merged;
}

/** How many agents mount the KB — the server count when present (it includes
 *  agents whose names workspace isolation hides), else the visible names. */
export function attachedAgentCount(kb: {
  attached_agents: string[];
  attached_agent_count?: number;
}): number {
  return Math.max(kb.attached_agent_count ?? 0, kb.attached_agents.length);
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

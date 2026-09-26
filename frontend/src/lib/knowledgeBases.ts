/**
 * Managed knowledge bases — wire types and pure rules shared by the classic
 * `/knowledge-bases` page and the native V2 page (`v2/pages/knowledge/`).
 */

export type KBStatus = "CREATING" | "ACTIVE" | "FAILED" | "DELETING";

export interface KnowledgeBaseSummary {
  kb_id: string;
  name: string;
  description: string;
  status: KBStatus | string;
  updated_at: string | null;
  data_source_count: number;
  attached_agents: string[];
}

export interface IngestionJob {
  job_id: string;
  status: string;
  started_at?: string | null;
  updated_at?: string | null;
  statistics?: Record<string, number>;
  failure_reasons?: string[];
}

export interface DataSource {
  ds_id: string;
  name: string;
  status: string;
  bucket: string | null;
  prefix?: string | null;
  failure_reasons?: string[];
  ingestion_jobs?: IngestionJob[];
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
  attached_agents: string[];
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

/** Connectors shown as "coming soon" next to the two S3 modes (v1 is S3-only). */
export const KB_COMING_SOON = ["webCrawler", "sharepoint", "confluence", "googleDrive", "oneDrive"] as const;

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

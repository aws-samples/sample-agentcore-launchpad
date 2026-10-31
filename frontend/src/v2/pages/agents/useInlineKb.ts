import { useCallback, useEffect, useRef, useState } from "react";

import { errorMessage, v2KnowledgeApi } from "../../../lib/api";
import type { KnowledgeBaseDetail } from "../../../lib/knowledgeBases";
import type { TagTone } from "../../ui";
import type { SectionProps } from "./wizardKit";

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

const POLL_MS = 5000;

/** Where an inline KB is on its way from "just created" to "answering". */
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

/** Whether the phase still changes on its own (worth polling). */
const inFlight = (p: InlineKbPhase) =>
  p === "creating" || p === "sourcePending" || p === "starting" || p === "indexing" || p === "unknown";

/** Deploy needs the KB itself to exist as ACTIVE (its Retrieve target is created at
 *  deploy); ingestion still running never blocks. */
export function inlineKbBlock(inline: InlineKb | null, selected: string[]): "waiting" | "failed" | null {
  if (!inline || !inline.uploaded || !selected.includes(inline.kb_id)) return null;
  const phase = inlineKbPhase(inline.detail);
  if (phase === "failed") return "failed";
  return String(inline.detail?.status ?? "CREATING").toUpperCase() === "ACTIVE" ? null : "waiting";
}

export interface InlineKbApi {
  inline: InlineKb | null;
  busy: boolean;
  error: string | null;
  create: (name: string, files: File[]) => Promise<void>;
  retryUpload: () => Promise<void>;
  discard: () => void;
  /** a new agent with no project picked yet: the KB would land in the wrong project */
  needsProject: boolean;
}

/**
 * Creates the wizard's inline KB (POST KB, then upload the files) and follows it:
 * polls the detail, and starts the first ingestion once the data source is
 * available (the same auto-sync the KB detail page performs). The KB id is mounted
 * into the form only after both calls succeeded, so a failure never leaves a
 * half-mounted spec.
 *
 * `projectId` is the project the KB is created, filled and synced in — the create
 * wizard's picked one; null (an edit) is the console's, the agent's own. `ready`
 * false (a new agent without a project yet) refuses `create`.
 */
export function useInlineKb(set: SectionProps["set"], projectId: string | null = null, ready = true): InlineKbApi {
  const [inline, setInline] = useState<InlineKb | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  const synced = useRef(new Set<string>());
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const upload = useCallback(
    async (rec: InlineKb) => {
      try {
        await v2KnowledgeApi.uploadFiles(rec.kb_id, rec.files, projectId);
      } catch (err) {
        if (alive.current) setInline({ ...rec, uploadError: errorMessage(err) });
        return;
      }
      if (!alive.current) return;
      setInline({ ...rec, uploaded: true, uploadError: null });
      set((prev) => ({
        selectedKbs: prev.selectedKbs.includes(rec.kb_id) ? prev.selectedKbs : [...prev.selectedKbs, rec.kb_id],
      }));
    },
    [set, projectId],
  );

  const create = useCallback(
    async (name: string, files: File[]) => {
      if (!ready) return;
      setBusy(true);
      setError(null);
      try {
        let detail: KnowledgeBaseDetail;
        try {
          detail = await v2KnowledgeApi.create({ name, description: "", source: { mode: "upload" } }, projectId);
        } catch (err) {
          if (alive.current) setError(errorMessage(err));
          return;
        }
        if (!alive.current) return;
        const rec: InlineKb = { kb_id: detail.kb_id, name, files, uploaded: false, uploadError: null, detail };
        setInline(rec);
        await upload(rec);
      } finally {
        if (alive.current) setBusy(false);
      }
    },
    [upload, ready, projectId],
  );

  const retryUpload = useCallback(async () => {
    if (!inline) return;
    setBusy(true);
    try {
      await upload(inline);
    } finally {
      if (alive.current) setBusy(false);
    }
  }, [inline, upload]);

  const discard = useCallback(() => {
    const id = inline?.kb_id;
    if (id) set((prev) => ({ selectedKbs: prev.selectedKbs.filter((k) => k !== id) }));
    setInline(null);
    setError(null);
  }, [inline, set]);

  // follow the KB until it is quiescent
  const followId = inline?.uploaded ? inline.kb_id : null;
  useEffect(() => {
    if (!followId) return;
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      let detail: KnowledgeBaseDetail | null = null;
      try {
        detail = await v2KnowledgeApi.get(followId, projectId);
      } catch {
        /* transient read failure — keep the last state and retry */
      }
      if (cancelled) return;
      let phase: InlineKbPhase = "unknown";
      if (detail) {
        const fresh = detail;
        setInline((prev) => (prev && prev.kb_id === followId ? { ...prev, detail: fresh } : prev));
        phase = inlineKbPhase(fresh);
        const source = fresh.data_sources[0];
        if (phase === "starting" && source && !synced.current.has(source.ds_id)) {
          synced.current.add(source.ds_id);
          try {
            await v2KnowledgeApi.sync(followId, source.ds_id, projectId);
          } catch {
            /* the KB page's manual "sync now" remains */
          }
          if (cancelled) return;
        }
      }
      if (inFlight(phase)) timer = window.setTimeout(() => void tick(), POLL_MS);
    };
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [followId, projectId]);

  return { inline, busy, error, create, retryUpload, discard, needsProject: !ready };
}

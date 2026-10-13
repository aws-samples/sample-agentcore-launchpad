import { useCallback, useEffect, useRef, useState } from "react";

import { errorMessage, v2KnowledgeApi } from "../../../lib/api";
import type { KnowledgeBaseDetail } from "../../../lib/knowledgeBases";
import {
  created,
  type InlineKb,
  type InlineKbPhase,
  inlineKbPhase,
  mountKb,
  phaseInFlight,
  POLL_MS,
  refreshed,
  syncTarget,
  unmountKb,
  uploadFailed,
  uploadSucceeded,
} from "./inlineKbState";
import type { SectionProps } from "./wizardKit";

export interface InlineKbApi {
  inline: InlineKb | null;
  /** the create / upload calls are running */
  busy: boolean;
  /** the create call failed (nothing exists yet) */
  error: string | null;
  create: (name: string, files: File[]) => Promise<void>;
  retryUpload: () => Promise<void>;
  /** unmount the KB from this agent; the KB itself is kept (never deleted) */
  discard: () => void;
}

/**
 * Creates the wizard's inline KB (POST KB, then upload the files) and follows it:
 * polls the detail, and starts the first ingestion once the data source is
 * available (the same auto-sync the KB detail page performs). The KB id is mounted
 * into the form only after both calls succeeded, so a failure never leaves a
 * half-mounted spec.
 *
 * Nothing here deletes the KB: discarding it or leaving the wizard keeps it as an
 * ordinary workspace knowledge base.
 */
export function useInlineKb(set: SectionProps["set"]): InlineKbApi {
  const [inline, setInline] = useState<InlineKb | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  // one create / upload at a time, even before the `busy` re-render lands
  const running = useRef(false);
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
        await v2KnowledgeApi.uploadFiles(rec.kb_id, rec.files);
      } catch (err) {
        if (alive.current) setInline(uploadFailed(rec, errorMessage(err)));
        return;
      }
      if (!alive.current) return;
      setInline(uploadSucceeded(rec));
      set((prev) => ({ selectedKbs: mountKb(prev.selectedKbs, rec.kb_id) }));
    },
    [set],
  );

  const create = useCallback(
    async (name: string, files: File[]) => {
      if (running.current) return;
      running.current = true;
      setBusy(true);
      setError(null);
      try {
        let detail: KnowledgeBaseDetail;
        try {
          detail = await v2KnowledgeApi.create({ name, description: "", source: { mode: "upload" } });
        } catch (err) {
          if (alive.current) setError(errorMessage(err));
          return;
        }
        if (!alive.current) return;
        const rec = created(name, files, detail);
        setInline(rec);
        await upload(rec);
      } finally {
        running.current = false;
        if (alive.current) setBusy(false);
      }
    },
    [upload],
  );

  const retryUpload = useCallback(async () => {
    if (!inline || running.current) return;
    running.current = true;
    setBusy(true);
    try {
      await upload(inline);
    } finally {
      running.current = false;
      if (alive.current) setBusy(false);
    }
  }, [inline, upload]);

  const discard = useCallback(() => {
    const id = inline?.kb_id;
    if (id) set((prev) => ({ selectedKbs: unmountKb(prev.selectedKbs, id) }));
    setInline(null);
    setError(null);
  }, [inline, set]);

  // follow the KB until it is quiescent; the timer dies with the record or the page
  const followId = inline?.uploaded ? inline.kb_id : null;
  useEffect(() => {
    if (!followId) return;
    let cancelled = false;
    let timer: number | undefined;
    const tick = async () => {
      let detail: KnowledgeBaseDetail | null = null;
      try {
        detail = await v2KnowledgeApi.get(followId);
      } catch {
        /* transient read failure — keep the last state and retry */
      }
      if (cancelled) return;
      let phase: InlineKbPhase = "unknown";
      if (detail) {
        const fresh = detail;
        setInline((prev) => refreshed(prev, followId, fresh));
        phase = inlineKbPhase(fresh);
        const dsId = syncTarget(fresh, synced.current);
        if (dsId) {
          synced.current.add(dsId);
          try {
            await v2KnowledgeApi.sync(followId, dsId);
          } catch {
            // stop following: nothing starts ingestion now but the KB page's manual sync
            if (!cancelled) setInline((prev) => (prev && prev.kb_id === followId ? { ...prev, syncFailed: true } : prev));
            return;
          }
          if (cancelled) return;
        }
      }
      if (phaseInFlight(phase)) timer = window.setTimeout(() => void tick(), POLL_MS);
    };
    void tick();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [followId]);

  return { inline, busy, error, create, retryUpload, discard };
}

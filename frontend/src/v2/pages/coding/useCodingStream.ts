import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, codingApi, errorMessage, localizedMessage } from "../../../lib/api";
import { applyCodingEvents, type CodingEventRecord, type ConversationState, emptyConversation } from "./codingEvents";
import { afterStream, FLUSH_MS, isTerminalStreamError, shouldPause, sseFrameBatches, type StreamEnd } from "./stream";

/** The stream's connection state, for the run-status line. */
export type StreamPhase = "connecting" | "live" | "retrying" | "paused" | "stopped";

/** The first frame of every stream request: the ledger's view of the project. */
export interface StreamStatus {
  projectStatus: string | null;
  activeTurnId: string | null;
}

function statusOf(data: unknown): StreamStatus {
  const d = (data && typeof data === "object" ? data : {}) as Record<string, unknown>;
  const active = d.active_turn;
  let activeTurnId: string | null = null;
  if (typeof active === "string") activeTurnId = active;
  else if (active && typeof active === "object") {
    const a = active as Record<string, unknown>;
    const id = a.turn_id ?? a.turnId ?? a.id;
    activeTurnId = typeof id === "string" ? id : null;
  }
  return { projectStatus: typeof d.project_status === "string" ? d.project_status : null, activeTurnId };
}

function isRecord(value: unknown): value is CodingEventRecord {
  if (!value || typeof value !== "object") return false;
  const r = value as Record<string, unknown>;
  return typeof r.seq === "number" && typeof r.method === "string";
}

/**
 * The project's live conversation: one `GET …/events?after=<seq>` after another.
 * A request replays everything after the cursor (compact history on the first one)
 * and then tails live; `reconnect`, a dropped connection or an error starts the next
 * request after the last applied seq (backoff on failures, `stream.ts`). A tab hidden
 * for a minute stops streaming until it is visible again; unmounting aborts. The turn
 * itself never depends on this connection.
 */
export function useCodingStream(
  projectId: string,
  enabled: boolean,
  onRecords?: (records: CodingEventRecord[]) => void,
) {
  const [conversation, setConversation] = useState<ConversationState>(emptyConversation);
  const [status, setStatus] = useState<StreamStatus | null>(null);
  const [phase, setPhase] = useState<StreamPhase>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const cursor = useRef(0);
  // received but not yet applied (the cursor is already past them); kept across effect
  // runs so a restart applies what the previous run queued
  const queue = useRef<CodingEventRecord[]>([]);
  const onRecordsRef = useRef(onRecords);
  onRecordsRef.current = onRecords;

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let paused = false;
    let controller: AbortController | null = null;
    let wake: (() => void) | null = null;
    let hiddenSince: number | null = document.hidden ? Date.now() : null;

    // records wait up to FLUSH_MS and are applied as one batch (one render), so a
    // burst of deltas does not re-render the IDE once per network chunk
    let flushTimer: number | undefined;
    const flush = () => {
      flushTimer = undefined;
      const batch = queue.current;
      if (batch.length === 0) return;
      queue.current = [];
      setConversation((prev) => applyCodingEvents(prev, batch));
      onRecordsRef.current?.(batch);
    };
    const enqueue = (records: CodingEventRecord[]) => {
      for (const record of records) queue.current.push(record);
      flushTimer ??= window.setTimeout(flush, FLUSH_MS);
    };
    if (queue.current.length) flushTimer = window.setTimeout(flush, 0);

    const sleep = (ms: number) =>
      new Promise<void>((resolve) => {
        const done = () => {
          window.clearTimeout(timer);
          wake = null;
          resolve();
        };
        const timer = window.setTimeout(done, ms);
        wake = done;
      });

    const onVisibility = () => {
      if (document.hidden) {
        hiddenSince = Date.now();
        return;
      }
      hiddenSince = null;
      paused = false;
      wake?.(); // a visible tab retries now instead of waiting out its backoff
    };
    document.addEventListener("visibilitychange", onVisibility);
    const pauseTimer = window.setInterval(() => {
      if (!paused && shouldPause(hiddenSince, Date.now())) {
        paused = true;
        controller?.abort();
      }
    }, 5000);

    const run = async () => {
      let failures = 0;
      while (!cancelled) {
        if (paused) {
          setPhase("paused");
          await sleep(60 * 60 * 1000); // until visible again (onVisibility wakes)
          continue;
        }
        controller = new AbortController();
        let end: StreamEnd = "ended";
        let got = false;
        let terminal: string | null = null;
        try {
          setPhase(failures ? "retrying" : "connecting");
          const res = await codingApi.events(projectId, cursor.current, controller.signal);
          if (cancelled) return;
          setPhase("live");
          for await (const batch of sseFrameBatches(res)) {
            const records: CodingEventRecord[] = [];
            for (const frame of batch) {
              if (frame.event === "event" && isRecord(frame.data)) {
                records.push(frame.data);
              } else if (frame.event === "status") {
                setStatus(statusOf(frame.data));
                setError(null);
              } else if (frame.event === "reconnect") {
                end = "reconnect";
              } else if (frame.event === "error") {
                end = "error";
                const d = (frame.data ?? {}) as { code?: string; message?: string };
                const code = d.code ?? "coding.runtime_error";
                setError(localizedMessage(code, d.message ?? code));
                if (isTerminalStreamError(code)) terminal = code;
              }
            }
            const fresh = records.filter((r) => r.seq > cursor.current);
            if (fresh.length) {
              got = true;
              for (const r of fresh) if (r.seq > cursor.current) cursor.current = r.seq;
              enqueue(fresh);
              setError(null);
            }
            if (end !== "ended") break;
          }
        } catch (err) {
          if (cancelled) return;
          if (paused) continue;
          end = "failed";
          if (err instanceof ApiError && isTerminalStreamError(err.code)) terminal = err.code;
          setError(errorMessage(err));
        } finally {
          controller.abort(); // release the connection after a `reconnect` / `error` frame
        }
        if (cancelled) return;
        if (terminal) {
          setPhase("stopped");
          return;
        }
        const next = afterStream(end, failures, got);
        failures = next.failures;
        if (next.delayMs > 0) {
          setPhase("retrying");
          await sleep(next.delayMs);
        }
      }
    };
    void run();

    return () => {
      cancelled = true;
      controller?.abort();
      wake?.();
      window.clearTimeout(flushTimer); // a re-run applies what is still queued
      window.clearInterval(pauseTimer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [projectId, enabled, nonce]);

  /** Start streaming again after a terminal stop (the Retry action). */
  const restart = useCallback(() => {
    setError(null);
    setNonce((n) => n + 1);
  }, []);

  return { conversation, status, phase, error, restart };
}

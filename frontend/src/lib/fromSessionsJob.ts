import { ApiError, type V2FromSessionsJob, type V2FromSessionsOutcome } from "./api";

/**
 * Consecutive failed polls the "add to dataset" modal tolerates before it stops and shows
 * the error (r8-a2 review note C). One missed read is not a failed job, but an endless
 * retry hides a backend that went away; at 1 s a poll this is ~30 s of silence.
 */
export const JOB_POLL_MAX_CONSECUTIVE_ERRORS = 30;

/**
 * Whether the poller gives up after its `consecutiveErrors`-th failed read in a row:
 * at once when the job row itself is gone (`job.not_found`), otherwise at the ceiling.
 * A successful read resets the caller's counter.
 */
export function shouldStopPolling(consecutiveErrors: number, err: unknown, max = JOB_POLL_MAX_CONSECUTIVE_ERRORS): boolean {
  if (err instanceof ApiError && err.code === "job.not_found") return true;
  return consecutiveErrors >= max;
}

/** The modal's reading of a from-sessions job row (N-SCN-obs10 round-6). */
export type FromSessionsPhase = "queued" | "reading" | "writing" | "succeeded" | "failed";

export interface FromSessionsView {
  phase: FromSessionsPhase;
  /** span reads completed / asked (0 / 0 before the first progress write) */
  read: number;
  total: number;
  /** 0..100, for the bar */
  percent: number;
  result: V2FromSessionsOutcome | null;
  /** the AppError message on failure (`job.error` as a fallback) */
  error: string | null;
  errorCode: string | null;
  done: boolean;
}

export function fromSessionsJobView(job: V2FromSessionsJob | null | undefined): FromSessionsView {
  const payload = job?.payload ?? {};
  const progress = payload.progress ?? { read: 0, total: payload.session_ids?.length ?? 0 };
  const total = Math.max(0, progress.total);
  const read = Math.min(total, Math.max(0, progress.read));
  const percent = total > 0 ? Math.round((read / total) * 100) : 0;
  if (!job) {
    return { phase: "queued", read, total, percent, result: null, error: null, errorCode: null, done: false };
  }
  if (job.status === "succeeded") {
    return { phase: "succeeded", read: total, total, percent: 100, result: payload.result ?? null, error: null, errorCode: null, done: true };
  }
  if (job.status === "failed") {
    return {
      phase: "failed", read, total, percent, result: null,
      error: payload.error?.message ?? job.error ?? null,
      errorCode: payload.error?.code ?? null,
      done: true,
    };
  }
  if (job.status === "queued") {
    return { phase: "queued", read, total, percent, result: null, error: null, errorCode: null, done: false };
  }
  // running: every read done → the extraction + write is what remains
  const phase: FromSessionsPhase = total > 0 && read >= total ? "writing" : "reading";
  return { phase, read, total, percent, result: null, error: null, errorCode: null, done: false };
}

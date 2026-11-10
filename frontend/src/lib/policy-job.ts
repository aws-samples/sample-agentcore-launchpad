// D20 P1-b: recognising a `policy.compile_failed` deploy in a job log. The deploy pipeline
// records a failed stage as `"<ExcType>: <message>"` (and the same text as `job.error`), so
// the error CODE never reaches the log — only the backend's message does. This mirrors
// `policy_enforcement.narrowing_for`'s 409 message ("the policy set could not decide this
// agent's Gateway tools — the deploy did not proceed …") and also accepts the code itself,
// should a future pipeline write it.
import type { JobInfo } from "./api";

const COMPILE_FAILED_RE = /policy\.compile_failed|policy set could not decide/i;

/** True for a log line / job error produced by a `policy.compile_failed` (409) deploy stop. */
export function isPolicyCompileFailure(text: string | null | undefined): boolean {
  return !!text && COMPILE_FAILED_RE.test(text);
}

/** True when the job stopped on the policy compile gate: `job.error` or an `error`-level
 *  event carries the signature. Info / debug lines (the traceback) never qualify alone. */
export function jobPolicyCompileFailed(job: Pick<JobInfo, "error" | "events"> | null | undefined): boolean {
  if (!job) return false;
  if (isPolicyCompileFailure(job.error)) return true;
  return job.events.some((event) => event.level === "error" && isPolicyCompileFailure(event.msg));
}

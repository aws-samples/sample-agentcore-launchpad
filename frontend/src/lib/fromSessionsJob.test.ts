import { describe, expect, it } from "vitest";

import { ApiError, type V2FromSessionsJob } from "./api";
import { fromSessionsJobView, JOB_POLL_MAX_CONSECUTIVE_ERRORS, shouldStopPolling } from "./fromSessionsJob";

/** N-SCN-obs10 round-6 (r8-a): how the "add to dataset" modal reads the job it polls. */
function job(status: V2FromSessionsJob["status"], payload: V2FromSessionsJob["payload"], error: string | null = null): V2FromSessionsJob {
  return { id: "j1", type: "eval_dataset_from_sessions", status, error, events: [], payload };
}

describe("fromSessionsJobView", () => {
  it("is queued with the asked total before the job starts (and before any row exists)", () => {
    expect(fromSessionsJobView(null)).toMatchObject({ phase: "queued", read: 0, total: 0, percent: 0, done: false });
    const v = fromSessionsJobView(job("queued", { session_ids: ["a", "b", "c"], progress: { read: 0, total: 3 } }));
    expect(v).toMatchObject({ phase: "queued", read: 0, total: 3, percent: 0, done: false });
  });

  it("reads progress while running and switches to writing once every span read is done", () => {
    expect(fromSessionsJobView(job("running", { progress: { read: 5, total: 12 } }))).toMatchObject({
      phase: "reading", read: 5, total: 12, percent: 42, done: false,
    });
    expect(fromSessionsJobView(job("running", { progress: { read: 12, total: 12 } }))).toMatchObject({
      phase: "writing", read: 12, total: 12, percent: 100, done: false,
    });
  });

  it("hands over the result on success", () => {
    const result = { dataset: { id: "d1", name: "big", kind: "predefined", item_count: 12 }, added: 12, skipped: [] };
    const v = fromSessionsJobView(job("succeeded", { progress: { read: 12, total: 12 }, result }));
    expect(v).toMatchObject({ phase: "succeeded", percent: 100, done: true, result, error: null });
  });

  it("surfaces the AppError envelope on failure, falling back to job.error", () => {
    const v = fromSessionsJobView(job("failed", {
      progress: { read: 1, total: 1 },
      error: { code: "dataset.nothing_extracted", message: "none of the selected sessions produced a usable exchange" },
    }, "none of the selected sessions produced a usable exchange"));
    expect(v).toMatchObject({ phase: "failed", done: true, errorCode: "dataset.nothing_extracted", result: null });
    expect(v.error).toContain("none of the selected sessions");
    const swept = fromSessionsJobView(job("failed", {}, "interrupted by a backend restart — run FROM SESSIONS again"));
    expect(swept.error).toContain("restart");
    expect(swept.errorCode).toBeNull();
  });

  it("never reports more reads than the total", () => {
    expect(fromSessionsJobView(job("running", { progress: { read: 9, total: 4 } }))).toMatchObject({ read: 4, total: 4, percent: 100, phase: "writing" });
  });
});

describe("shouldStopPolling (r8-a2: the modal's poll loop has a ceiling)", () => {
  it("stops at once when the job row is gone", () => {
    const gone = new ApiError("job.not_found", "job not found", null);
    expect(shouldStopPolling(1, gone)).toBe(true);
  });

  it("keeps polling through transient failures until the consecutive-error ceiling", () => {
    const transient = new Error("network");
    for (let n = 1; n < JOB_POLL_MAX_CONSECUTIVE_ERRORS; n++) expect(shouldStopPolling(n, transient)).toBe(false);
    expect(shouldStopPolling(JOB_POLL_MAX_CONSECUTIVE_ERRORS, transient)).toBe(true);
    expect(JOB_POLL_MAX_CONSECUTIVE_ERRORS).toBe(30);
  });

  it("treats any non-404 ApiError like a transient failure (a 503 may clear)", () => {
    const busy = new ApiError("service.unavailable", "try again", null);
    expect(shouldStopPolling(1, busy)).toBe(false);
    expect(shouldStopPolling(3, busy, 3)).toBe(true);
  });
});

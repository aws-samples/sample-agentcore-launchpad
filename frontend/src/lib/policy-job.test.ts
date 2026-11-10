import { describe, expect, it } from "vitest";

import { isPolicyCompileFailure, jobPolicyCompileFailed } from "./policy-job";

// the pipeline writes `"<ExcType>: <message>"` for a failed stage — the backend's 409
// message, not the `policy.compile_failed` code, is what reaches Job.log / job.error
const STAGE_LINE =
  "AppError: the policy set could not decide this agent's Gateway tools — the deploy did not proceed with an unknown tool surface";

describe("isPolicyCompileFailure", () => {
  it("matches the backend message and the error code", () => {
    expect(isPolicyCompileFailure(STAGE_LINE)).toBe(true);
    expect(isPolicyCompileFailure("deploy failed: policy.compile_failed")).toBe(true);
  });
  it("ignores other failures and empty text", () => {
    expect(isPolicyCompileFailure("ClientError: AccessDeniedException")).toBe(false);
    expect(isPolicyCompileFailure("")).toBe(false);
    expect(isPolicyCompileFailure(null)).toBe(false);
  });
});

describe("jobPolicyCompileFailed", () => {
  const ev = (level: string, msg: string) => ({ ts: "2026-10-14T03:00:00Z", stage: "provision", level, msg });
  it("reads job.error or an error-level event", () => {
    expect(jobPolicyCompileFailed({ error: STAGE_LINE, events: [] })).toBe(true);
    expect(jobPolicyCompileFailed({ error: null, events: [ev("info", "generating"), ev("error", STAGE_LINE)] })).toBe(true);
  });
  it("does not fire on the debug traceback alone, on other errors, or without a job", () => {
    expect(jobPolicyCompileFailed({ error: null, events: [ev("debug", STAGE_LINE)] })).toBe(false);
    expect(jobPolicyCompileFailed({ error: "RuntimeError: boom", events: [ev("error", "RuntimeError: boom")] })).toBe(false);
    expect(jobPolicyCompileFailed(null)).toBe(false);
  });
});

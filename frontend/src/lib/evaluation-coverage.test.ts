import { describe, expect, it } from "vitest";

import { coverageDiffers, type EvaluationRunInfo, type EvaluationSessionFailures, runCoverage, runRecommendable } from "./evaluation";

const SIDS = Array.from({ length: 16 }, (_, i) => `s${i + 1}`);

function run(fields: Partial<EvaluationRunInfo>): EvaluationRunInfo {
  return {
    id: "r1", agent_id: "a1", agent_name: "agent", dataset_name: "ds", mode: "evaluators",
    evaluators: [], status: "completed", queue_position: null, scores: [], insights: {},
    session_ids: SIDS, batch_eval_id: "run_r1", error: null, ...fields,
  } as EvaluationRunInfo;
}

function failures(kind: EvaluationSessionFailures["kind"], failed: number, indexes: number[]): EvaluationSessionFailures {
  return {
    total: 16, failed, kind,
    sessions: indexes.map((index) => ({
      session_id: `s${index}`, index, kind: kind === "evaluator_error" ? "evaluator_error" : "telemetry_incomplete",
      error_type: "LogEventMissingException", message: "Session span data is incomplete.",
    })),
  };
}

const PARTIAL = (n: number) => `${n} of 16 sessions failed during batch evaluation.`;

describe("runCoverage", () => {
  it("is null for a clean run", () => {
    expect(runCoverage(run({}))).toBeNull();
  });

  it("reads the stored summary", () => {
    const cov = runCoverage(run({ error: PARTIAL(5), session_failures: failures("telemetry_incomplete", 5, [9, 2, 4, 5, 13]) }));
    expect(cov).toMatchObject({ total: 16, scored: 11, failed: 5, kind: "telemetry_incomplete", legacy: false });
    expect(cov?.excluded).toEqual([2, 4, 5, 9, 13]);
    expect(cov?.firstError).toBe("LogEventMissingException: Session span data is incomplete.");
  });

  it("parses the AWS sentence on a legacy row", () => {
    expect(runCoverage(run({ error: PARTIAL(2) }))).toMatchObject({ total: 16, scored: 14, kind: "unknown", legacy: true });
  });

  it("ignores an unrelated completed-run error", () => {
    expect(runCoverage(run({ error: "insufficient samples for clustering" }))).toBeNull();
  });
});

describe("runRecommendable", () => {
  it("accepts a clean run with a batch", () => {
    expect(runRecommendable(run({})).ok).toBe(true);
    expect(runRecommendable(run({ batch_eval_id: null })).reason).toBe("not_completed");
  });

  it("accepts a telemetry-only partial run that scored at least half", () => {
    const res = runRecommendable(run({ error: PARTIAL(8), session_failures: failures("telemetry_incomplete", 8, [1]) }));
    expect(res.ok).toBe(true);
    expect(res.coverage?.scored).toBe(8);
  });

  it("refuses low coverage, evaluator errors and legacy rows", () => {
    expect(runRecommendable(run({ error: PARTIAL(9), session_failures: failures("telemetry_incomplete", 9, [1]) })).reason).toBe("low_coverage");
    expect(runRecommendable(run({ error: PARTIAL(1), session_failures: failures("evaluator_error", 1, [1]) })).reason).toBe("evaluator_error");
    expect(runRecommendable(run({ error: PARTIAL(1), session_failures: failures("mixed", 1, [1]) })).reason).toBe("evaluator_error");
    expect(runRecommendable(run({ error: PARTIAL(2) })).reason).toBe("legacy");
    expect(runRecommendable(run({ error: PARTIAL(2), session_failures: failures("unknown", 2, []) })).reason).toBe("unknown");
  });
});

describe("coverageDiffers", () => {
  const cov = (indexes: number[]) => runCoverage(run({ error: PARTIAL(indexes.length), session_failures: failures("telemetry_incomplete", indexes.length, indexes) }));

  it("flags runs that left different scenarios unscored", () => {
    expect(coverageDiffers([cov([9, 13]), null, cov([4, 6])])).toBe(true);
  });

  it("is quiet for one partial run or identical gaps", () => {
    expect(coverageDiffers([cov([9, 13]), null])).toBe(false);
    expect(coverageDiffers([cov([2]), cov([2])])).toBe(false);
  });
});

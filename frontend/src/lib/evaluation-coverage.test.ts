import { describe, expect, it } from "vitest";

import {
  coverageDiffers,
  coverageInfo,
  type EvaluationRunInfo,
  type EvaluationSessionFailures,
  runCoverage,
  runRecommendable,
} from "./evaluation";

/** N12-obs47 (upstream 00eb359b / 19f1ac7e parity, on CN's per-session summary): how a
 *  partially failed run reads, and when it may seed a recommendation. */

const SIDS = Array.from({ length: 16 }, (_, i) => `s${i + 1}`);

function run(fields: Partial<EvaluationRunInfo>): EvaluationRunInfo {
  return {
    id: "r1", agent_id: "a1", agent_name: "agent", dataset_name: "ds", mode: "evaluators",
    evaluators: [], status: "completed", queue_position: null, scores: [], insights: {},
    session_ids: SIDS, batch_eval_id: "run_r1", error: null, ...fields,
  } as EvaluationRunInfo;
}

function failures(kind: EvaluationSessionFailures["kind"], failed: number, indexes: number[]): EvaluationSessionFailures {
  const sessionKind = kind === "mixed" ? "telemetry_incomplete" : kind;
  return {
    total: 16, failed, kind,
    sessions: indexes.map((index) => ({
      session_id: `s${index}`, index, kind: sessionKind,
      error_type: "TelemetryReadinessTimeout",
      message: "scenario q answered but its telemetry never became ready: TelemetryReadinessTimeout: no spans",
    })),
  };
}

const PARTIAL = (n: number) => `${n} of 16 evaluator records errored — first error: RuntimeError: x`;
const RECORDS = (count: number) => ({ count, total: 16, first_type: "RuntimeError", first_message: "x" });

describe("runCoverage", () => {
  it("keeps each unscored scenario's arm (N12-obs62) and has none on a plain / legacy run", () => {
    const f = failures("replay_failed", 2, [3, 7]);
    f.sessions[0].arm = "T1";
    f.sessions[1].arm = null; // the hub had no arm for this session (plain run)
    const cov = runCoverage(run({ error: PARTIAL(2), session_failures: f }));
    expect(cov?.excluded).toEqual([3, 7]);
    expect(cov?.arms).toEqual({ 3: "T1" });
    // an older backend sends no `arm` at all → nothing attributed, nothing breaks
    expect(runCoverage(run({ error: PARTIAL(2), session_failures: failures("replay_failed", 2, [3, 7]) }))?.arms).toEqual({});
    expect(runCoverage(run({ error: PARTIAL(2), errors: RECORDS(2) }))?.arms).toEqual({});
  });

  it("is null for a clean run, a failed run and an unrelated completed-run error", () => {
    expect(runCoverage(run({}))).toBeNull();
    expect(runCoverage(run({ status: "failed", error: "all 16 evaluator records failed" }))).toBeNull();
    expect(runCoverage(run({ error: "insufficient samples for clustering" }))).toBeNull();
  });

  it("reads the stored per-session summary", () => {
    const cov = runCoverage(run({ error: PARTIAL(5), session_failures: failures("telemetry_incomplete", 5, [9, 2, 4, 5, 13]) }));
    expect(cov).toMatchObject({ total: 16, scored: 11, failed: 5, kind: "telemetry_incomplete", legacy: false, records: null });
    expect(cov?.excluded).toEqual([2, 4, 5, 9, 13]);
    expect(cov?.firstError).toMatch(/^TelemetryReadinessTimeout: scenario q answered/);
    expect(coverageInfo(cov)).toBe(true);
    expect(coverageInfo(runCoverage(run({ error: PARTIAL(1), session_failures: failures("evaluator_error", 1, [1]) })))).toBe(false);
  });

  it("reads a pre-summary row (record counts only) as legacy", () => {
    const cov = runCoverage(run({ error: PARTIAL(2), errors: RECORDS(2) }));
    expect(cov).toMatchObject({ total: 16, kind: "unknown", legacy: true, records: { count: 2, total: 16 } });
    expect(cov?.firstError).toBe("RuntimeError: x");
  });
});

describe("runRecommendable", () => {
  it("accepts a clean run with a job", () => {
    expect(runRecommendable(run({})).ok).toBe(true);
    expect(runRecommendable(run({ batch_eval_id: null })).reason).toBe("not_completed");
    expect(runRecommendable(run({ status: "evaluating" })).reason).toBe("not_completed");
  });

  it("accepts a telemetry-only partial run that scored at least half", () => {
    const res = runRecommendable(run({ error: PARTIAL(8), session_failures: failures("telemetry_incomplete", 8, [1]) }));
    expect(res.ok).toBe(true);
    expect(res.coverage?.scored).toBe(8);
  });

  it("refuses low coverage, replay failures, evaluator errors and legacy rows", () => {
    expect(runRecommendable(run({ error: PARTIAL(9), session_failures: failures("telemetry_incomplete", 9, [1]) })).reason).toBe("low_coverage");
    expect(runRecommendable(run({ error: PARTIAL(1), session_failures: failures("replay_failed", 1, [1]) })).reason).toBe("replay_failed");
    expect(runRecommendable(run({ error: PARTIAL(1), session_failures: failures("evaluator_error", 1, [1]) })).reason).toBe("evaluator_error");
    expect(runRecommendable(run({ error: PARTIAL(1), session_failures: failures("mixed", 1, [1]) })).reason).toBe("evaluator_error");
    expect(runRecommendable(run({ error: PARTIAL(2), errors: RECORDS(2) })).reason).toBe("legacy");
    // an error with neither summary nor record counts: nothing to reason from
    expect(runRecommendable(run({ error: PARTIAL(2) })).reason).toBe("unknown");
  });
});

describe("coverageDiffers", () => {
  const cov = (indexes: number[]) =>
    runCoverage(run({ error: PARTIAL(indexes.length), session_failures: failures("telemetry_incomplete", indexes.length, indexes) }));

  it("flags runs that left different scenarios unscored", () => {
    expect(coverageDiffers([cov([9, 13]), null, cov([4, 6])])).toBe(true);
  });

  it("is quiet for one partial run, identical gaps or legacy rows", () => {
    expect(coverageDiffers([cov([9, 13]), null])).toBe(false);
    expect(coverageDiffers([cov([2]), cov([2])])).toBe(false);
    expect(coverageDiffers([cov([2]), runCoverage(run({ error: PARTIAL(2), errors: RECORDS(2) }))])).toBe(false);
  });
});

import { describe, expect, it } from "vitest";

import { filterResults, type ResultRow, rowsFromOnline, rowsFromRun, sortResults, summarize } from "./results";
import type { V2Task } from "./tasks";

function row(over: Partial<ResultRow>): ResultRow {
  return {
    key: over.key ?? "k",
    time: null,
    taskKey: "run:t1",
    taskName: "nightly",
    agent: "hr-bot",
    agentId: "a1",
    source: "dataset",
    sessionId: null,
    traceId: null,
    evaluatorId: "Builtin.Helpfulness",
    level: "TRACE",
    score: null,
    normalized: null,
    label: null,
    explanation: null,
    error: null,
    outcome: "error",
    ...over,
  };
}

const ROWS = [
  row({ key: "a", time: "2026-10-09T08:00:00Z", score: 0.9, normalized: 0.9, outcome: "passed", label: "Very Helpful", sessionId: "sess-a" }),
  row({ key: "b", time: "2026-10-09T09:00:00Z", score: 0.2, normalized: 0.2, outcome: "failed", explanation: "Refused the leave question", evaluatorId: "Builtin.Correctness" }),
  row({ key: "c", time: null, error: "judge timed out", outcome: "error" }),
  row({ key: "d", time: "2026-10-09T07:00:00Z", score: 0.5, normalized: 0.5, outcome: "failed", sessionId: "sess-d" }),
];
const keys = (rows: ResultRow[]) => rows.map((r) => r.key);

describe("filterResults", () => {
  it("narrows by evaluator, outcome and score band", () => {
    expect(keys(filterResults(ROWS, { evaluator: "Builtin.Correctness" }))).toEqual(["b"]);
    expect(keys(filterResults(ROWS, { outcome: "failed" }))).toEqual(["b", "d"]);
    expect(keys(filterResults(ROWS, { band: "mid" }))).toEqual(["d"]);
    // an unscored row is in no band
    expect(keys(filterResults(ROWS, { band: "low" }))).toEqual(["b"]);
    expect(keys(filterResults([row({ key: "p", normalized: 1 })], { band: "high" }))).toEqual(["p"]);
  });

  it("searches label, explanation, error and session case-insensitively", () => {
    expect(keys(filterResults(ROWS, { q: "very" }))).toEqual(["a"]);
    expect(keys(filterResults(ROWS, { q: "LEAVE" }))).toEqual(["b"]);
    expect(keys(filterResults(ROWS, { q: "timed out" }))).toEqual(["c"]);
    expect(keys(filterResults(ROWS, { q: " sess-d " }))).toEqual(["d"]);
    expect(keys(filterResults(ROWS, { q: "" }))).toEqual(["a", "b", "c", "d"]);
  });
});

describe("sortResults", () => {
  it("keeps the incoming order without a sort", () => {
    expect(sortResults(ROWS, null)).toBe(ROWS);
  });

  it("sorts scores both ways with unscored rows last", () => {
    expect(keys(sortResults(ROWS, { key: "norm", dir: "asc" }))).toEqual(["b", "d", "a", "c"]);
    expect(keys(sortResults(ROWS, { key: "norm", dir: "desc" }))).toEqual(["a", "d", "b", "c"]);
  });

  it("sorts time, and outcome puts Bad Cases first ascending", () => {
    expect(keys(sortResults(ROWS, { key: "time", dir: "desc" }))).toEqual(["b", "a", "d", "c"]);
    expect(keys(sortResults(ROWS, { key: "outcome", dir: "asc" }))).toEqual(["b", "d", "c", "a"]);
  });

  it("is stable on ties", () => {
    expect(keys(sortResults(ROWS, { key: "evaluator", dir: "asc" }))).toEqual(["b", "a", "c", "d"]);
  });
});

describe("rowsFromRun", () => {
  it("carries the judged turn, call, reconciliation and judge of every row (N17-obs317)", () => {
    const task = { kind: "run", id: "r1", name: "nightly", agentName: "hr-bot", agentId: "a1", source: "dataset", updatedAt: null } as unknown as V2Task;
    const rows = rowsFromRun(task, {
      sessions: [
        {
          session_id: "s1",
          results: [
            {
              evaluator_id: "Builtin.ToolSelectionAccuracy", level: "TOOL_CALL", score: 1, label: "Yes", explanation: null,
              error_type: null, error_message: null, trace_id: "t1", span_id: "sp1", tool_name: "kb_search",
              extraction_status: "ok", judge_model_id: "global.zai.glm-5.3",
            },
          ],
          scenario_id: "refund-1",
          scenario_label: "How do I get a refund?",
        },
      ],
    } as never);
    expect(rows[0]).toMatchObject({ traceId: "t1", spanId: "sp1", toolName: "kb_search", extractionStatus: "ok", judgeModelId: "global.zai.glm-5.3" });
    // N17-obs391: the session's dataset scenario
    expect(rows[0]).toMatchObject({ scenarioId: "refund-1", scenarioLabel: "How do I get a refund?" });
  });
});

describe("rowsFromRun carries the failure class (R3-f)", () => {
  it("maps failure_class onto the row, null on a judged row", () => {
    const task = { kind: "run", id: "r1", name: "run", updatedAt: "t", agentName: "a", agentId: "a1",
      source: "run" } as unknown as Parameters<typeof rowsFromRun>[0];
    const row = (failure_class: string | null) => ({
      evaluator_id: "Builtin.Helpfulness", level: "TRACE", score: failure_class ? null : 0.8,
      label: failure_class ? null : "Good", explanation: null,
      error_type: failure_class ? "ReadTimeoutError" : null, error_message: null, failure_class,
    });
    const rows = rowsFromRun(task, { sessions: [{ session_id: "s1", results: [row(null), row("timeout")] }] } as never);
    expect(rows.map((r) => r.failureClass)).toEqual([null, "timeout"]);
  });
});

describe("per-evaluator pass bar (N17-obs425)", () => {
  const task = { kind: "run", id: "r1", name: "nightly", agentName: "hr-bot", agentId: "a1", source: "dataset", updatedAt: null } as unknown as V2Task;
  const result = (evaluator_id: string, score: number) => ({ evaluator_id, level: "TOOL_CALL", score, label: null, explanation: null, error_type: null, error_message: null });

  it("passes SkillInstructionFollowing only on Fully Followed", () => {
    const sif = "Builtin.SkillInstructionFollowing";
    const rows = rowsFromRun(task, { sessions: [{ session_id: "s1", results: [1, 0.75, 0.5, 0.25, 0].map((s) => result(sif, s)) }] } as never);
    expect(rows.map((r) => r.outcome)).toEqual(["passed", "failed", "failed", "failed", "failed"]);
    const summary = summarize(rows);
    expect([summary.passed, summary.failed]).toEqual([1, 4]);
    expect(summary.byEvaluator[0].failed).toBe(4);
  });

  it("keeps 0.7 for multi-point scales and 1 for two-label ones, Refusal inverted", () => {
    const rows = rowsFromRun(task, {
      sessions: [{
        session_id: "s1",
        results: [result("Builtin.Helpfulness", 0.833), result("Builtin.Helpfulness", 0.667), result("Builtin.ResponseRelevance", 0.75), result("Builtin.Refusal", 0), result("Builtin.Refusal", 1), result("custom-judge", 0.7)],
      }],
    } as never);
    expect(rows.map((r) => r.outcome)).toEqual(["passed", "failed", "passed", "passed", "failed", "passed"]);
  });

  it("passes Faithfulness only on Completely Yes; the score itself is unchanged", () => {
    const rows = rowsFromRun(task, { sessions: [{ session_id: "s1", results: [1, 0.75].map((s) => result("Builtin.Faithfulness", s)) }] } as never);
    expect(rows.map((r) => [r.score, r.outcome])).toEqual([[1, "passed"], [0.75, "failed"]]);
  });

  it("passes Coherence only on Completely Yes; the score itself is unchanged", () => {
    const rows = rowsFromRun(task, { sessions: [{ session_id: "s1", results: [1, 0.75].map((s) => result("Builtin.Coherence", s)) }] } as never);
    expect(rows.map((r) => [r.score, r.outcome])).toEqual([[1, "passed"], [0.75, "failed"]]);
  });

  it("applies the same bar to online results", () => {
    const rows = rowsFromOnline(task, [
      { evaluator_id: "Builtin.SkillInstructionFollowing", score: 0.75, session_id: "s", trace_id: null, level: "TOOL_CALL", label: "Mostly Followed", explanation: null, error: null, time: null },
    ] as never);
    expect(rows[0].outcome).toBe("failed");
  });
});

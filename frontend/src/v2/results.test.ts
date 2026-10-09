import { describe, expect, it } from "vitest";

import { filterResults, type ResultRow, sortResults } from "./results";

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

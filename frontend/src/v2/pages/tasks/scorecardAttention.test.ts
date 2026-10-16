import { describe, expect, it } from "vitest";

import type { EvaluationScorecard, RunBenchmark, ScorecardAgent, ScorecardRun } from "../../../lib/api";
import type { EvaluationRunStatus } from "../../../lib/evaluation";
import { FAILED_RUN_TTL_MS, HINT_MAX, SCORECARD_ATTENTION_CAP, scorecardAttention as attentionAt } from "./scorecardAttention";

const C = "Builtin.Correctness";
const H = "Builtin.Helpfulness";
const T = "Builtin.ToolSelectionAccuracy";

function bench(key: string, revision: string | null = null): RunBenchmark {
  if (key === "live") return { kind: "live", key, label: null, dataset_id: null, revision: null, size: null };
  const [prefix, id] = key.split(":");
  return { kind: prefix === "cloud" ? "cloud" : "dataset", key, label: id, dataset_id: id, revision, size: 20 };
}

// "now" for every case: the fixtures run from 2026-10-01 00:00 UTC, an hour apart
const NOW = Date.UTC(2026, 9, 3);
const scorecardAttention = (sc: EvaluationScorecard) => attentionAt(sc, NOW);
const DAY = 24 * 60 * 60 * 1000;
/** a naive ledger timestamp (UTC, no offset) `ms` before NOW */
const ago = (ms: number) => new Date(NOW - ms).toISOString().slice(0, 23);

let n = 0;
function run(
  id: string,
  agent: string,
  o: {
    bench?: RunBenchmark;
    seq?: number | null;
    scores?: Record<string, number>;
    status?: EvaluationRunStatus;
    error?: string;
    created?: string;
  } = {},
): ScorecardRun {
  n += 1;
  const b = o.bench ?? bench("cloud:faq", "v3");
  const scores = o.scores ?? {};
  return {
    id,
    name: `run ${id}`,
    agent_key: agent,
    status: o.status ?? "completed",
    error: o.error ?? null,
    created_at: o.created ?? new Date(Date.UTC(2026, 9, 1, n)).toISOString().slice(0, 19),
    updated_at: null,
    evaluators: Object.keys(scores).length ? Object.keys(scores) : [C, H, T],
    scores: Object.entries(scores).map(([evaluatorId, score]) => ({ evaluatorId, score })),
    sessions: 20,
    scope: b.kind === "live" ? "window" : b.kind === "cloud" ? "cloud" : "dataset",
    dataset_name: b.kind === "live" ? "window:24h" : b.label,
    dataset_version: null,
    benchmark: b,
    snapshot: o.seq === null ? null : { seq: o.seq ?? 1, source: "stamped" },
    created_by_name: "lin",
  };
}

function agent(key: string, over: Partial<ScorecardAgent> = {}): ScorecardAgent {
  return { key, agent_id: key, name: key, method: "container", log_source: null, deleted: false, current_seq: 5, versions: [], ...over };
}

const card = (runs: ScorecardRun[], agents: ScorecardAgent[]): EvaluationScorecard => ({ runs, agents, truncated: false });

// store-service-assistant, as the prototype: #3 → #4 improved, a failed re-test at #4,
// then #5 (a model swap) regressed on the same cloud dataset version.
function store(): ScorecardRun[] {
  return [
    run("s1", "store", { seq: 3, scores: { [C]: 0.78, [H]: 0.8, [T]: 0.85 } }),
    run("s2", "store", { seq: 4, scores: { [C]: 0.86, [H]: 0.84, [T]: 0.9 } }),
    run("s3", "store", { seq: 4, status: "failed", error: "replay timed out\non case 14" }),
    run("s4", "store", { seq: 5, scores: { [C]: 0.68, [H]: 0.79, [T]: 0.71 } }),
  ];
}

describe("scorecard attention", () => {
  it("is empty when nothing regressed or failed", () => {
    const runs = [
      run("a", "ok", { seq: 1, scores: { [C]: 0.6 } }),
      run("b", "ok", { seq: 2, scores: { [C]: 0.7 } }),
    ];
    expect(scorecardAttention(card(runs, [agent("ok")]))).toEqual([]);
    expect(scorecardAttention(card([], []))).toEqual([]);
  });

  it("raises a regression against the previous version's latest run, with the two worst evaluators", () => {
    // the #4 failure (s3) is no longer listed: s4 completed on the same benchmark after it
    const entries = scorecardAttention(card(store(), [agent("store")]));
    expect(entries).toHaveLength(1);
    const [reg] = entries;
    expect(reg).toMatchObject({ kind: "regression", signal: "act", from: 4, to: 5, compositeDrop: true });
    if (reg.kind === "failed") throw new Error("unexpected");
    expect(reg.previous.id).toBe("s2");
    expect(reg.latest.id).toBe("s4");
    expect(reg.composite.delta).toBeCloseTo((0.68 + 0.79 + 0.71 - (0.86 + 0.84 + 0.9)) / 3);
    expect(reg.worst.map((c) => c.evaluatorId)).toEqual([T, C]);
    const retest = run("s5", "store", { seq: 5, status: "failed", error: "replay timed out\non case 14" });
    const [, failed] = scorecardAttention(card([...store(), retest], [agent("store")]));
    expect(failed).toMatchObject({ key: "failed:s5", kind: "failed", signal: "act", detail: "replay timed out" });
  });

  it("raises on a single evaluator's fall of 0.1 even with a flat composite", () => {
    const runs = [
      run("a", "x", { seq: 1, scores: { [C]: 0.7, [H]: 0.5 } }),
      run("b", "x", { seq: 2, scores: { [C]: 0.6, [H]: 0.6 } }),
    ];
    const [entry] = scorecardAttention(card(runs, [agent("x")]));
    expect(entry).toMatchObject({ kind: "regression", compositeDrop: false });
    if (entry.kind === "failed") throw new Error("unexpected");
    expect(entry.worst.map((c) => c.evaluatorId)).toEqual([C]);
  });

  it("waits for confirmation when the two runs replayed different dataset revisions", () => {
    const runs = [
      run("a", "x", { bench: bench("ds:qa", "f1"), seq: 1, scores: { [C]: 0.9 } }),
      run("b", "x", { bench: bench("ds:qa", "f2"), seq: 2, scores: { [C]: 0.7 } }),
    ];
    const [entry] = scorecardAttention(card(runs, [agent("x")]));
    expect(entry).toMatchObject({ kind: "pending", signal: "wait" });
    // an unknown (legacy) revision on one side is not a known change
    const legacy = [
      run("c", "y", { bench: bench("ds:qa", null), seq: 1, scores: { [C]: 0.9 } }),
      run("d", "y", { bench: bench("ds:qa", "f2"), seq: 2, scores: { [C]: 0.7 } }),
    ];
    expect(scorecardAttention(card(legacy, [agent("y")]))[0]).toMatchObject({ kind: "regression", signal: "act" });
  });

  it("never raises on a same-version re-run, on live traffic, on a log source or a deleted agent", () => {
    const sameVersion = [
      run("a", "x", { seq: 2, scores: { [C]: 0.9 } }),
      run("b", "x", { seq: 2, scores: { [C]: 0.5 } }),
    ];
    expect(scorecardAttention(card(sameVersion, [agent("x")])).length).toBe(0);
    const live = [
      run("c", "y", { bench: bench("live"), seq: 1, scores: { [H]: 0.9 } }),
      run("d", "y", { bench: bench("live"), seq: 2, scores: { [H]: 0.5 } }),
    ];
    expect(scorecardAttention(card(live, [agent("y")])).length).toBe(0);
    const logs = [
      run("e", "logs:svc", { bench: bench("live"), seq: null, scores: { [H]: 0.9 } }),
      run("f", "logs:svc", { bench: bench("live"), seq: null, scores: { [H]: 0.4 } }),
    ];
    expect(scorecardAttention(card(logs, [agent("logs:svc", { agent_id: null })])).length).toBe(0);
    expect(scorecardAttention(card(store(), [agent("store", { deleted: true })])).length).toBe(0);
  });

  it("stays quiet just under the thresholds", () => {
    const runs = [
      run("a", "x", { seq: 1, scores: { [C]: 0.75, [H]: 0.75 } }),
      run("b", "x", { seq: 2, scores: { [C]: 0.66, [H]: 0.75 } }), // C −0.09, composite −0.045
    ];
    expect(scorecardAttention(card(runs, [agent("x")])).length).toBe(0);
  });

  it("ranks regressions, then failures (newest first), then the wait rows; caps and counts", () => {
    n = 0;
    const runs: ScorecardRun[] = [
      run("p1", "pend", { bench: bench("ds:qa", "f1"), seq: 1, scores: { [C]: 0.9 } }),
      run("p2", "pend", { bench: bench("ds:qa", "f2"), seq: 2, scores: { [C]: 0.6 } }),
      run("f1", "store", { status: "failed" }),
      run("r1", "reg", { seq: 1, scores: { [C]: 0.9 } }),
      run("r2", "reg", { seq: 2, scores: { [C]: 0.5 } }),
      ...Array.from({ length: 5 }, (_, i) => run(`fx${i}`, "store", { status: "failed", error: "x".repeat(200) })),
    ];
    const entries = scorecardAttention(card(runs, [agent("store"), agent("reg"), agent("pend")]));
    expect(entries.map((e) => e.key)).toEqual([
      "regression:reg:cloud:faq",
      "failed:fx4",
      "failed:fx3",
      "failed:fx2",
      "failed:fx1",
      "failed:fx0",
      "failed:f1",
      "pending:pend:ds:qa",
    ]);
    // the panel shows the first SCORECARD_ATTENTION_CAP and counts all eight
    expect(entries.slice(0, SCORECARD_ATTENTION_CAP).map((e) => e.key).at(-1)).toBe("failed:fx1");
    const all = scorecardAttention(card(runs.slice(0, 5), [agent("store"), agent("reg"), agent("pend")]));
    expect(all.map((e) => e.kind)).toEqual(["regression", "failed", "pending"]);
    const clipped = entries[1];
    if (clipped.kind !== "failed") throw new Error("unexpected");
    expect(clipped.detail).toHaveLength(HINT_MAX);
    expect(clipped.detail.endsWith("…")).toBe(true);
  });

  describe("failed run expiry", () => {
    const failedKeys = (runs: ScorecardRun[], agents = [agent("x"), agent("y")]) =>
      scorecardAttention(card(runs, agents))
        .filter((e) => e.kind === "failed")
        .map((e) => e.key);
    const done = (id: string, agentKey: string, b?: RunBenchmark) => run(id, agentKey, { bench: b, scores: { [C]: 0.8 } });

    it("lists a failed run for 7 days after it was created, not a moment longer", () => {
      expect(failedKeys([run("edge", "x", { status: "failed", created: ago(FAILED_RUN_TTL_MS) })])).toEqual(["failed:edge"]);
      expect(failedKeys([run("old", "x", { status: "failed", created: ago(FAILED_RUN_TTL_MS + 1) })])).toEqual([]);
      expect(failedKeys([run("zoned", "x", { status: "failed", created: new Date(NOW - DAY).toISOString() })])).toEqual(["failed:zoned"]);
      // a time that cannot be read is no proof of age: kept
      const unknown = { ...run("none", "x", { status: "failed" }), created_at: null };
      expect(failedKeys([unknown])).toEqual(["failed:none"]);
    });

    it("drops a failure once a later run of the same agent on the same benchmark completes", () => {
      const runs = [run("f", "x", { status: "failed" }), done("ok", "x")];
      expect(failedKeys(runs)).toEqual([]);
      // a completion before the failure fixed nothing
      expect(failedKeys([...runs].reverse())).toEqual(["failed:f"]);
    });

    it("keeps a failure that a later run only failed again or stopped", () => {
      const runs = [
        run("f1", "x", { status: "failed" }),
        run("f2", "x", { status: "failed" }),
        run("st", "x", { status: "stopped" }),
      ];
      expect(failedKeys(runs)).toEqual(["failed:f2", "failed:f1"]);
    });

    it("is not superseded by a completed run on another benchmark or of another agent", () => {
      const failed = run("f", "x", { status: "failed", bench: bench("ds:qa", "f1") });
      expect(failedKeys([failed, done("other-ds", "x", bench("ds:faq", "f1"))])).toEqual(["failed:f"]);
      expect(failedKeys([failed, done("cloud", "x", bench("cloud:qa", "v1"))])).toEqual(["failed:f"]);
      expect(failedKeys([failed, done("other-agent", "y", bench("ds:qa", "f1"))])).toEqual(["failed:f"]);
      // the same dataset at another revision is still the same benchmark
      expect(failedKeys([failed, done("rev", "x", bench("ds:qa", "f2"))])).toEqual([]);
    });

    it("treats live traffic as one benchmark per agent", () => {
      const failed = run("w", "x", { status: "failed", bench: bench("live") });
      expect(failedKeys([failed, done("w2", "x", bench("live"))])).toEqual([]);
    });
  });
});

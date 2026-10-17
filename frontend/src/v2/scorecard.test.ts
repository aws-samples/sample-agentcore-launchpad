import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type {
  EvaluationScorecard,
  RunBenchmark,
  RunSnapshotSource,
  ScorecardAgent,
  ScorecardRun,
  ScorecardVersion,
  SpecChangeGroup,
} from "../lib/api";
import type { EvaluationRunStatus } from "../lib/evaluation";
import { backTarget, backState, returnPath, returnState } from "./pages/tasks/back";
import {
  agentMethod,
  agentMethods,
  agentRow,
  agentRows,
  changeKind,
  benchEvaluators,
  benchmarksOf,
  changesBetween,
  comparison,
  composite,
  coreEvaluators,
  evaluatorChanges,
  filterAgentRows,
  headline,
  isActive,
  isRegression,
  isScored,
  matrixColumns,
  olderVersion,
  previousValue,
  recordsVersions,
  rerunnable,
  rerunSource,
  revisionMarkers,
  runMean,
  runSteps,
  seriesColor,
  seriesSlots,
  shortDate,
  shortTime,
  togglePick,
  versionBands,
  versionMatrix,
  windowHours,
} from "./scorecard";

const C = "Builtin.Correctness";
const H = "Builtin.Helpfulness";
const G = "Builtin.GoalSuccessRate";
const I = "Builtin.InstructionFollowing";
const R = "Builtin.Refusal"; // a penalty evaluator: raw 1.0 = refused = bad

function bench(key: string, over: Partial<RunBenchmark> = {}): RunBenchmark {
  if (key === "live") return { kind: "live", key, label: null, dataset_id: null, revision: null, size: null, ...over };
  const [prefix, id] = key.split(":");
  return { kind: prefix === "cloud" ? "cloud" : "dataset", key, label: id, dataset_id: id, revision: null, size: null, ...over };
}

let minute = 0;
/** Runs get increasing creation times in the order they are built (oldest first). */
function run(
  id: string,
  o: {
    agent?: string;
    bench?: RunBenchmark;
    seq?: number | null;
    source?: RunSnapshotSource;
    scores?: Record<string, number>;
    evaluators?: string[];
    status?: EvaluationRunStatus;
    scope?: ScorecardRun["scope"];
    dataset_name?: string | null;
    error?: string | null;
  } = {},
): ScorecardRun {
  minute += 1;
  const b = o.bench ?? bench("ds:qa");
  const scores = o.scores ?? {};
  const seq = o.seq === undefined ? 1 : o.seq;
  const scope = o.scope ?? (b.kind === "live" ? "window" : b.kind === "cloud" ? "cloud" : "dataset");
  return {
    id,
    name: `run ${id}`,
    agent_key: o.agent ?? "ag1",
    status: o.status ?? "completed",
    error: o.error ?? null,
    // naive UTC, as the ledger writes it
    created_at: new Date(Date.UTC(2026, 9, 1, 0, minute * 30)).toISOString().slice(0, 19),
    updated_at: null,
    evaluators: o.evaluators ?? Object.keys(scores),
    scores: Object.entries(scores).map(([evaluatorId, score]) => ({ evaluatorId, score })),
    sessions: 3,
    scope,
    dataset_name: o.dataset_name !== undefined ? o.dataset_name : scope === "window" ? "window:24h" : b.label,
    dataset_version: null,
    benchmark: b,
    snapshot: seq == null ? null : { seq, source: o.source ?? "stamped" },
    created_by_name: "river",
  };
}

function agent(key: string, over: Partial<ScorecardAgent> = {}): ScorecardAgent {
  return {
    key,
    agent_id: key.startsWith("logs:") ? null : key,
    name: key,
    method: "harness",
    log_source: null,
    deleted: false,
    current_seq: 1,
    versions: [],
    ...over,
  };
}

function version(seq: number, changes: SpecChangeGroup[] = []): ScorecardVersion {
  return { seq, aws_version: String(seq), created_at: null, created_by_name: "river", note: null, changes };
}

// hr-assistant, as the reviewed prototype: four versions on hr-policy-qa, the dataset
// grew from 12 to 16 cases at #3 (revision f1 → f2), InstructionFollowing joined at #3,
// a same-version noise re-run at #3, an edge-case benchmark and live-traffic windows.
const QA1 = bench("ds:qa", { revision: "f1", size: 12, label: "hr-policy-qa" });
const QA2 = bench("ds:qa", { revision: "f2", size: 16, label: "hr-policy-qa" });
const EDGE = bench("ds:edge", { revision: "e1", size: 6, label: "hr-edge-cases" });
const LIVE = bench("live");
function hrRuns(): ScorecardRun[] {
  return [
    run("h1", { bench: QA1, seq: 1, source: "inferred", scores: { [C]: 0.5, [H]: 0.6, [G]: 0.4 } }),
    run("h2", { bench: QA1, seq: 1, source: "inferred", scores: { [C]: 0.55, [H]: 0.62, [G]: 0.45 } }),
    run("h3", { bench: QA1, seq: 2, scores: { [C]: 0.7, [H]: 0.7, [G]: 0.6 } }),
    run("hl1", { bench: LIVE, seq: 2, source: "observed", scores: { [H]: 0.72, [G]: 0.61 } }),
    run("h5", { bench: QA2, seq: 3, scores: { [C]: 0.8, [H]: 0.78, [G]: 0.7, [I]: 0.6 } }),
    run("he1", { bench: EDGE, seq: 3, scores: { [G]: 0.5, [H]: 0.6 } }),
    run("h7", { bench: QA2, seq: 3, scores: { [C]: 0.82, [H]: 0.8, [G]: 0.72, [I]: 0.62 } }),
    run("hl2", { bench: LIVE, seq: 3, source: "observed", scores: { [H]: 0.79, [G]: 0.71 } }),
    run("h9", { bench: QA2, seq: 4, scores: { [C]: 0.9, [H]: 0.85, [G]: 0.8, [I]: 0.7 } }),
    run("he2", { bench: EDGE, seq: 4, scores: { [G]: 0.8, [H]: 0.75 } }),
  ];
}

describe("run states and means", () => {
  it("scores a completed run with scores only", () => {
    expect(isScored(run("a", { scores: { [C]: 1 } }))).toBe(true);
    expect(isScored(run("b", { scores: {} }))).toBe(false);
    expect(isScored(run("c", { status: "failed", scores: { [C]: 1 } }))).toBe(false);
    expect(["queued", "invoking", "waiting", "evaluating"].every((s) => isActive({ status: s as EvaluationRunStatus }))).toBe(true);
    expect(isActive({ status: "completed" })).toBe(false);
    expect(isActive({ status: "stopped" })).toBe(false);
  });

  it("runMean is the unweighted mean of normalized evaluator means", () => {
    expect(runMean(run("a", { scores: { [C]: 0.8, [R]: 0.2 } }))).toBeCloseTo(0.8); // (0.8 + (1 - 0.2)) / 2
    expect(runMean({ scores: [{ evaluatorId: C, score: 1, count: 10 }, { evaluatorId: H, score: 0, count: 1 }] })).toBe(0.5);
    expect(runMean(run("b"))).toBeNull();
  });
});

describe("benchmarks", () => {
  it("orders the main benchmark first: most scored runs, ties to the most recent", () => {
    const benches = benchmarksOf(hrRuns());
    // edge and live tie on two scored runs; edge's newest run is the more recent
    expect(benches.map((b) => [b.key, b.scored.length])).toEqual([
      ["ds:qa", 6],
      ["ds:edge", 2],
      ["live", 2],
    ]);
    // the newest run's label wins
    expect(benches[0].label).toBe("hr-policy-qa");
  });

  it("does not count failed or active runs toward the main benchmark", () => {
    const runs = [
      run("a", { bench: bench("ds:x"), scores: { [C]: 1 } }),
      run("b", { bench: bench("ds:y"), status: "failed" }),
      run("c", { bench: bench("ds:y"), status: "queued" }),
    ];
    expect(benchmarksOf(runs)[0].key).toBe("ds:x");
    // with no scored run at all, the newest run's benchmark leads
    expect(benchmarksOf(runs.slice(1))[0].key).toBe("ds:y");
  });

  it("the composite core is what every scored run includes; an evaluator added mid-way stays out", () => {
    const qa = benchmarksOf(hrRuns())[0];
    expect(coreEvaluators(qa.runs)).toEqual([C, H, G]);
    expect(benchEvaluators(qa.runs)).toEqual([C, H, G, I]);
    const h9 = qa.runs.find((r) => r.id === "h9") as ScorecardRun;
    expect(composite(h9, [C, H, G])).toBeCloseTo((0.9 + 0.85 + 0.8) / 3);
    // an unscored run has no composite
    expect(composite(run("q", { status: "queued", evaluators: [C] }), [C])).toBeNull();
  });

  it("the composite flips a penalty evaluator and skips a core evaluator the run did not score", () => {
    const r = run("a", { scores: { [C]: 0.6, [R]: 0.2 } });
    expect(composite(r, [C, R])).toBeCloseTo(0.7);
    const partial = run("b", { evaluators: [C, H], scores: { [C]: 0.4 } });
    expect(composite(partial, [C, H])).toBeCloseTo(0.4);
  });
});

describe("comparison and headline", () => {
  it("pairs the latest run with the newest run of an older version, skipping same-version re-runs", () => {
    const qa = benchmarksOf(hrRuns())[0];
    const cmp = comparison(qa.scored);
    expect(cmp?.latest.id).toBe("h9");
    expect(cmp?.previous?.id).toBe("h7");
    expect(cmp?.byVersion).toBe(true);
  });

  it("a run without a version (a log source) is compared with the run before it", () => {
    const runs = [run("l1", { bench: LIVE, seq: null, scores: { [H]: 0.6 } }), run("l2", { bench: LIVE, seq: null, scores: { [H]: 0.7 } })];
    const head = headline(benchmarksOf(runs)[0], { agentScored: 2, anyComparable: true });
    expect(head.previous?.id).toBe("l1");
    expect(head.byVersion).toBe(false);
    expect(head.delta).toBeCloseTo(0.1);
    expect(head.signal).toBe("ok");
  });

  it("headline delta and signal over the bench composite", () => {
    const qa = benchmarksOf(hrRuns())[0];
    const head = headline(qa, { agentScored: 10, anyComparable: true });
    const expected = (0.9 + 0.85 + 0.8) / 3 - (0.82 + 0.8 + 0.72) / 3;
    expect(head.delta).toBeCloseTo(expected);
    expect(head.signal).toBe("ok");
    expect(head.reason).toBeNull();
  });

  it("says why there is no delta", () => {
    const ctx = (agentScored: number, anyComparable: boolean) => ({ agentScored, anyComparable });
    expect(headline(null, ctx(0, false)).reason).toBe("noCompleted");
    const failedOnly = benchmarksOf([run("f", { status: "failed" })])[0];
    expect(headline(failedOnly, ctx(0, false)).reason).toBe("noCompleted");
    const single = benchmarksOf([run("s", { scores: { [C]: 1 } })])[0];
    expect(headline(single, ctx(1, false)).reason).toBe("single");
    // two runs, each on its own dataset: no benchmark can pair
    expect(headline(single, ctx(2, false)).reason).toBe("differentDatasets");
    // a lone run here while another benchmark has a trend is just "one run"
    expect(headline(single, ctx(5, true)).reason).toBe("single");
    const sameVersion = benchmarksOf([run("v1", { scores: { [C]: 1 } }), run("v2", { scores: { [C]: 0.5 } })])[0];
    const head = headline(sameVersion, ctx(2, true));
    expect(head.reason).toBe("noEarlierVersion");
    expect(head.delta).toBeNull();
    expect(head.signal).toBeNull();
    // a pair whose runs share no evaluator has no composite to compare
    const disjoint = benchmarksOf([run("d1", { seq: 1, scores: { [C]: 0.9 } }), run("d2", { seq: 2, scores: { [H]: 0.4 } })])[0];
    const apart = headline(disjoint, ctx(2, true));
    expect(apart.previous?.id).toBe("d1");
    expect(apart).toMatchObject({ core: [], score: null, delta: null, reason: "noSharedScore", signal: null });
  });

  it("signals a regression at the thresholds exactly, and stays quiet just under them", () => {
    const pair = (a: Record<string, number>, b: Record<string, number>) =>
      headline(benchmarksOf([run("a", { seq: 1, scores: a }), run("b", { seq: 2, scores: b })])[0], { agentScored: 2, anyComparable: true });
    expect(pair({ [C]: 0.75 }, { [C]: 0.7 }).signal).toBe("act"); // composite −0.05
    expect(pair({ [C]: 0.75 }, { [C]: 0.701 }).signal).toBe("info"); // −0.049
    // composite flat, one evaluator −0.1
    const split = pair({ [C]: 0.7, [H]: 0.5 }, { [C]: 0.6, [H]: 0.6 });
    expect(split.delta).toBeCloseTo(0);
    expect(split.signal).toBe("act");
    expect(pair({ [C]: 0.7, [H]: 0.5 }, { [C]: 0.61, [H]: 0.59 }).signal).toBe("info"); // −0.09
    expect(pair({ [C]: 0.5 }, { [C]: 0.55 }).signal).toBe("ok"); // +0.05
    expect(pair({ [C]: 0.5 }, { [C]: 0.52 }).signal).toBe("info");
  });

  it("evaluator changes: shared and scored on both sides, largest fall first", () => {
    const a = run("a", { scores: { [C]: 0.8, [H]: 0.7, [G]: 0.5 } });
    const b = run("b", { scores: { [C]: 0.6, [H]: 0.75, [I]: 0.9 } });
    const changes = evaluatorChanges(a, b);
    expect(changes.map((c) => c.evaluatorId)).toEqual([C, H]);
    expect(changes[0].delta).toBeCloseTo(-0.2);
    expect(isRegression(0, changes)).toBe(true);
    expect(isRegression(-0.04, [])).toBe(false);
  });
});

describe("run steps", () => {
  it("measures a version change against the older version and a re-run against its own version", () => {
    const qa = benchmarksOf(hrRuns())[0];
    const steps = runSteps(qa, coreEvaluators(qa.runs));
    expect([...steps.keys()]).toEqual(["h1", "h2", "h3", "h5", "h7", "h9"]);
    expect(steps.get("h1")).toMatchObject({ previous: null, delta: null, sameVersion: false, regression: false });
    const h2 = steps.get("h2");
    expect(h2?.previous?.id).toBe("h1");
    expect(h2?.sameVersion).toBe(true);
    expect(h2?.delta).toBeCloseTo((0.55 + 0.62 + 0.45) / 3 - (0.5 + 0.6 + 0.4) / 3);
    // #2 is measured against #1's newest run, the re-run
    expect(steps.get("h3")?.previous?.id).toBe("h2");
    expect(steps.get("h3")?.sameVersion).toBe(false);
    expect(steps.get("h7")).toMatchObject({ sameVersion: true });
    expect(steps.get("h7")?.previous?.id).toBe("h5");
    // the latest run pairs as the headline does
    const head = headline(qa, { agentScored: 10, anyComparable: true });
    expect(steps.get("h9")?.previous?.id).toBe(head.previous?.id);
    expect(steps.get("h9")?.delta).toBeCloseTo(head.delta ?? NaN);
    expect(steps.get("h9")?.score).toBeCloseTo(head.score ?? NaN);
  });

  it("flags a regressing version change, never a same-version re-run", () => {
    const runs = [
      run("a", { seq: 1, scores: { [C]: 0.8, [H]: 0.6 } }),
      run("b", { seq: 1, scores: { [C]: 0.5, [H]: 0.5 } }),
      run("c", { seq: 2, scores: { [C]: 0.7, [H]: 0.6 } }),
      run("d", { seq: 3, scores: { [C]: 0.55, [H]: 0.8 } }),
    ];
    const qa = benchmarksOf(runs)[0];
    const steps = runSteps(qa, coreEvaluators(qa.runs));
    expect(steps.get("b")).toMatchObject({ sameVersion: true, regression: false });
    expect(steps.get("b")?.delta).toBeCloseTo(-0.2);
    expect(steps.get("c")?.regression).toBe(false);
    // composite +0.025, but Correctness −0.15
    expect(steps.get("d")?.delta).toBeCloseTo(0.025);
    expect(steps.get("d")?.regression).toBe(true);
  });

  it("reads runs from before the version records as older versions", () => {
    // the HR loop before snapshots existed: v1, v2 unrecorded, then the baseline #1
    const runs = [
      run("u1", { seq: null, scores: { [C]: 0.8, [H]: 0.92 } }),
      run("u2", { seq: null, scores: { [C]: 0.86, [H]: 0.94 } }),
      run("b1", { seq: 1, source: "inferred", scores: { [C]: 0.95, [H]: 0.95 } }),
    ];
    const qa = benchmarksOf(runs)[0];
    const steps = runSteps(qa, coreEvaluators(qa.runs));
    expect(steps.get("u1")?.previous).toBeNull();
    // two unrecorded runs pair in order, never as a same-version re-run
    expect(steps.get("u2")).toMatchObject({ sameVersion: false });
    expect(steps.get("u2")?.previous?.id).toBe("u1");
    expect(steps.get("b1")).toMatchObject({ sameVersion: false, regression: false });
    expect(steps.get("b1")?.previous?.id).toBe("u2");
    expect(steps.get("b1")?.delta).toBeCloseTo(0.95 - 0.9);
    const head = headline(qa, { agentScored: 3, anyComparable: true });
    expect(head).toMatchObject({ byVersion: true, reason: null, signal: "ok" });
    expect(head.previous?.id).toBe("u2");
    // the breakdown has the numbered version only
    expect(versionMatrix(qa.scored, coreEvaluators(qa.runs), [C, H]).map((r) => r.seq)).toEqual([1]);
    expect(olderVersion(runs[0], 1)).toBe(true);
    expect(olderVersion(runs[2], 1)).toBe(false);
  });

  it("an agent records versions once it has a deployed snapshot, a log source never", () => {
    expect(recordsVersions(agent("ag1", { versions: [version(1)] }))).toBe(true);
    expect(recordsVersions(agent("ag1", { versions: [] }))).toBe(false);
    expect(recordsVersions(agent("logs:svc", { versions: [version(1)], log_source: { service_name: "svc", log_group_names: [] } }))).toBe(false);
    expect(recordsVersions(null)).toBe(false);
  });

  it("skips unscored runs, and pairs runs without a version in order", () => {
    const runs = [
      run("l1", { bench: LIVE, seq: null, scores: { [H]: 0.6 } }),
      run("lf", { bench: LIVE, seq: null, status: "failed" }),
      run("lq", { bench: LIVE, seq: null, status: "queued" }),
      run("l2", { bench: LIVE, seq: null, scores: { [H]: 0.7 } }),
    ];
    const live = benchmarksOf(runs)[0];
    const steps = runSteps(live, coreEvaluators(live.runs));
    expect([...steps.keys()]).toEqual(["l1", "l2"]);
    expect(steps.get("l2")).toMatchObject({ sameVersion: false, regression: false });
    expect(steps.get("l2")?.previous?.id).toBe("l1");
    expect(steps.get("l2")?.delta).toBeCloseTo(0.1);
  });
});

describe("agent rows", () => {
  const sc = (runs: ScorecardRun[], agents: ScorecardAgent[]): EvaluationScorecard => ({ runs, agents, truncated: false });

  it("one row per agent with runs, most recently evaluated first", () => {
    minute = 0;
    const a1 = run("a1", { agent: "a", scores: { [C]: 0.8 } });
    const b1 = run("b1", { agent: "b", scores: { [C]: 0.6 } });
    const a2 = run("a2", { agent: "a", seq: 2, scores: { [C]: 0.7 } });
    const rows = agentRows(sc([a1, b1, a2], [agent("b"), agent("a"), agent("empty")]));
    expect(rows.map((r) => r.agent.key)).toEqual(["a", "b"]);
    expect(rows[0].newest?.id).toBe("a2");
    expect(rows[0].head.delta).toBeCloseTo(-0.1);
    expect(rows[0].head.signal).toBe("act");
    expect(rows[0].spark).toEqual([0.8, 0.7]);
    expect(rows[0].versions).toBe(2);
  });

  it("the sparkline and the headline read the main benchmark", () => {
    const row = agentRow(agent("hr", { current_seq: 4 }), hrRuns());
    expect(row.main?.key).toBe("ds:qa");
    expect(row.spark).toHaveLength(6);
    expect(row.spark[5]).toBeCloseTo(row.head.score as number);
    expect(row.versions).toBe(4);
    expect(row.benches).toHaveLength(3);
  });

  it("agents whose runs all used different datasets get the different-datasets reason", () => {
    const row = agentRow(agent("x"), [
      run("p", { bench: bench("ds:one"), scores: { [C]: 0.8 } }),
      run("q", { bench: bench("ds:two"), scores: { [C]: 0.4 } }),
    ]);
    expect(row.head.reason).toBe("differentDatasets");
    expect(row.head.delta).toBeNull();
  });
});

describe("agent row filters", () => {
  minute = 0;
  const rowOf = (key: string, scores: number[], over: Partial<ScorecardAgent> = {}) =>
    agentRow(agent(key, over), scores.map((v, i) => run(`${key}${i}`, { agent: key, seq: i + 1, scores: { [C]: v } })));
  const down = rowOf("down", [0.9, 0.6]);
  const up = rowOf("up", [0.5, 0.8]);
  const minor = rowOf("minor", [0.8, 0.79]);
  const single = rowOf("single", [0.7]);
  const gone = rowOf("gone", [0.9, 0.5], { deleted: true });
  const logs = rowOf("logs:svc", [0.6, 0.62], { method: null, log_source: { service_name: "svc", log_group_names: ["/g"] } });
  const rows = [down, up, minor, single, gone, logs];
  const keys = (list: typeof rows) => list.map((r) => r.agent.key);

  it("reads the change from the row's lamp", () => {
    expect([down, up, minor, single].map(changeKind)).toEqual(["down", "up", "minor", "none"]);
  });

  it("hides deleted agents for `active`, shows only them for `deleted`, all for \"\"", () => {
    expect(keys(filterAgentRows(rows, { status: "active", method: "", change: "" }))).not.toContain("gone");
    expect(keys(filterAgentRows(rows, { status: "deleted", method: "", change: "" }))).toEqual(["gone"]);
    expect(filterAgentRows(rows, { status: "", method: "", change: "" })).toHaveLength(rows.length);
  });

  it("filters by method (log sources as `logs`) and combines with the change filter", () => {
    expect(agentMethod(logs.agent)).toBe("logs");
    expect(agentMethod(down.agent)).toBe("harness");
    expect(agentMethods(rows)).toEqual(["harness", "logs"]);
    expect(keys(filterAgentRows(rows, { status: "", method: "logs", change: "" }))).toEqual(["logs:svc"]);
    expect(keys(filterAgentRows(rows, { status: "", method: "harness", change: "down" }))).toEqual(["down", "gone"]);
    expect(keys(filterAgentRows(rows, { status: "active", method: "harness", change: "down" }))).toEqual(["down"]);
  });
});

describe("trend bands and markers", () => {
  it("one band per consecutive version, with what changed since the previous band", () => {
    const qa = benchmarksOf(hrRuns())[0];
    const versions = [version(1), version(2, ["prompt"]), version(3, ["tools", "knowledge_bases"]), version(4, ["prompt"])];
    const bands = versionBands(qa.runs, versions);
    expect(bands.map((b) => [b.seq, b.start, b.end, b.approx])).toEqual([
      [1, 0, 1, true],
      [2, 2, 2, false],
      [3, 3, 4, false],
      [4, 5, 5, false],
    ]);
    expect(bands[0].changes).toEqual([]);
    expect(bands[2].changes).toEqual(["tools", "knowledge_bases"]);
  });

  it("a skipped version's changes fold into the next band; unknown groups read as other", () => {
    const runs = [run("a", { seq: 1 }), run("b", { seq: 3 }), run("c", { seq: null })];
    const bands = versionBands(runs, [version(2, ["model"]), version(3, ["prompt", "mystery" as SpecChangeGroup])]);
    expect(bands[1].changes).toEqual(["prompt", "model", "other"]);
    expect(bands[2]).toMatchObject({ seq: null, approx: false });
    expect(changesBetween([version(2, ["memory"])], 2, 4)).toEqual([]);
  });

  it("marks a dataset revision change on the first run of the new revision, skipping legacy rows", () => {
    const runs = [
      run("a", { bench: bench("ds:qa") }), // legacy: no revision
      run("b", { bench: QA1 }),
      run("c", { bench: bench("ds:qa") }),
      run("d", { bench: QA2 }),
      run("e", { bench: QA2 }),
    ];
    expect(revisionMarkers(runs)).toEqual([{ index: 3, fromSize: 12, toSize: 16 }]);
    expect(revisionMarkers(benchmarksOf(hrRuns())[0].runs).map((m) => m.index)).toEqual([3]);
  });

  it("series slots follow the sorted ids, so a filter or a benchmark switch never repaints", () => {
    const all = seriesSlots([H, C, G, I, R, "x1", "x2", "x3"]);
    expect([...all.entries()].slice(0, 3)).toEqual([
      [C, 0],
      [G, 1],
      [H, 2],
    ]);
    expect(all.get("x3")).toBeNull();
    expect(seriesColor(all.get(C))).toBe("var(--v2-series-1)");
    expect(seriesColor(null)).toBe("var(--v2-series-extra)");
  });

  it("a tooltip delta reads the nearest earlier point that has a value", () => {
    expect(previousValue([0.5, null, 0.7, null], 3)).toBe(0.7);
    expect(previousValue([null, 0.4], 1)).toBeNull();
  });
});

describe("version × evaluator", () => {
  it("one row per version, newest first, means over its runs and deltas vs the next older row", () => {
    const qa = benchmarksOf(hrRuns())[0];
    const core = coreEvaluators(qa.runs);
    const columns = matrixColumns(benchEvaluators(qa.runs), core);
    expect(columns).toEqual([C, H, G, I]);
    const rows = versionMatrix(qa.scored, core, columns);
    expect(rows.map((r) => [r.seq, r.runs, r.latest.id, r.older?.seq ?? null])).toEqual([
      [4, 1, "h9", 3],
      [3, 2, "h7", 2],
      [2, 1, "h3", 1],
      [1, 2, "h2", null],
    ]);
    // #3 is the mean of h5 and h7
    expect(rows[1].cells[0].value).toBeCloseTo(0.81);
    expect(rows[0].cells[0].delta).toBeCloseTo(0.9 - 0.81);
    // InstructionFollowing has no #2 value, so no delta on #3
    expect(rows[1].cells[3]).toMatchObject({ delta: null });
    expect(rows[1].cells[3].value).toBeCloseTo(0.61);
    expect(rows[3].composite.delta).toBeNull();
    expect(rows[1].approx).toBe(false);
    expect(rows[3].approx).toBe(true);
  });

  it("with one run per version the deltas equal the trend's point-to-point deltas", () => {
    const runs = [
      run("a", { seq: 1, scores: { [C]: 0.4, [H]: 0.6 } }),
      run("b", { seq: 2, scores: { [C]: 0.65, [H]: 0.55 } }),
      run("c", { seq: 3, scores: { [C]: 0.5, [H]: 0.9 } }),
    ];
    const core = coreEvaluators(runs);
    const rows = versionMatrix(runs, core, [C, H]);
    const compositeLine = runs.map((r) => composite(r, core));
    const cLine = runs.map((r) => composite(r, [C]));
    // the newest row against the next older one = the last point against the one before
    expect(rows[0].composite.delta).toBeCloseTo((compositeLine[2] as number) - (previousValue(compositeLine, 2) as number));
    expect(rows[1].cells[0].delta).toBeCloseTo((cLine[1] as number) - (previousValue(cLine, 1) as number));
  });

  it("caps the evaluator columns, the composite basis first", () => {
    expect(matrixColumns(["a", "b", "c", "d", "e"], ["e", "c"])).toEqual(["e", "c", "a", "b"]);
    expect(matrixColumns(["a"], ["z"])).toEqual(["a"]);
  });

  it("skips runs without a version", () => {
    expect(versionMatrix([run("a", { seq: null, scores: { [C]: 1 } })], [C], [C])).toEqual([]);
  });
});

describe("picks and re-run", () => {
  it("keeps at most two picks, in run order", () => {
    const order = ["r1", "r2", "r3"];
    expect(togglePick([], "r3", order)).toEqual(["r3"]);
    expect(togglePick(["r3"], "r1", order)).toEqual(["r1", "r3"]);
    expect(togglePick(["r1", "r3"], "r2", order)).toEqual(["r2", "r3"]); // the older pick drops
    expect(togglePick(["r1", "r3"], "r1", order)).toEqual(["r3"]);
  });

  it("re-runs dataset, cloud and window scopes only", () => {
    expect(windowHours("window:24h")).toBe(24);
    expect(windowHours("window:h")).toBeNull();
    expect(windowHours(null)).toBeNull();
    expect(rerunnable({ scope: "dataset", dataset_name: "qa" })).toBe(true);
    expect(rerunnable({ scope: "cloud", dataset_name: "cloud:qa" })).toBe(true);
    expect(rerunnable({ scope: "window", dataset_name: "window:72h" })).toBe(true);
    expect(rerunnable({ scope: "window", dataset_name: "window:" })).toBe(false);
    expect(rerunnable({ scope: "sessions", dataset_name: null })).toBe(false);
    expect(rerunnable({ scope: "logs", dataset_name: "logs:3" })).toBe(false);
  });

  it("re-runs the benchmark's newest completed run that can run again", () => {
    const live = benchmarksOf([
      run("w", { bench: LIVE, scores: { [H]: 0.5 } }),
      run("s", { bench: LIVE, scope: "sessions", dataset_name: null, scores: { [H]: 0.6 } }),
      run("q", { bench: LIVE, status: "queued" }),
    ])[0];
    expect(rerunSource(live, { deleted: false })).toEqual({ run: expect.objectContaining({ id: "w" }), block: null });
    const sessionsOnly = benchmarksOf([run("s2", { bench: LIVE, scope: "sessions", dataset_name: null, scores: { [H]: 1 } })])[0];
    expect(rerunSource(sessionsOnly, { deleted: false })).toEqual({ run: null, block: "unsupported" });
    const failed = benchmarksOf([run("f", { status: "failed" })])[0];
    expect(rerunSource(failed, { deleted: false })).toEqual({ run: null, block: "noRun" });
    expect(rerunSource(null, { deleted: false }).block).toBe("noRun");
    expect(rerunSource(live, { deleted: true }).block).toBe("deleted");
  });
});

describe("short times", () => {
  // run in UTC+8 whatever the host zone: in UTC a naive string reads the same either way
  const hostTz = process.env.TZ;
  beforeAll(() => {
    process.env.TZ = "Asia/Shanghai";
  });
  afterAll(() => {
    if (hostTz === undefined) delete process.env.TZ;
    else process.env.TZ = hostTz;
  });

  it("reads naive ledger times as UTC", () => {
    expect(shortDate("2026-10-15T16:30:00")).toBe("10-16");
    expect(shortTime("2026-10-15T16:30:00")).toBe("00:30");
    expect(shortDate("2026-10-15T16:30:00+00:00")).toBe("10-16");
    expect(shortDate(null)).toBe("—");
    expect(shortTime("garbage")).toBe("");
  });
});

describe("back target", () => {
  it("returns to the opener's search params, else the default tab", () => {
    expect(backTarget(backState("?tab=all")).toString()).toBe("tab=all");
    expect(backTarget(backState("?view=agent&key=a1&bench=ds%3Aqa")).get("bench")).toBe("ds:qa");
    expect(backTarget(null).toString()).toBe("");
    expect(backTarget({ other: 1 }).toString()).toBe("");
  });

  it("returns to an outside opener's in-app path only", () => {
    expect(returnPath(returnState("/v2/assistant?view=detail&id=c1"))).toBe("/v2/assistant?view=detail&id=c1");
    expect(returnPath(backState("?tab=all"))).toBeNull();
    expect(returnPath(null)).toBeNull();
    expect(returnPath({ returnTo: "https://example.com/" })).toBeNull();
    expect(returnPath({ returnTo: "//example.com/" })).toBeNull();
    expect(returnPath({ returnTo: 7 })).toBeNull();
  });
});

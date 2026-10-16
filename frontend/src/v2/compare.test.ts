import { describe, expect, it } from "vitest";

import type { RunBenchmark, RunCompare, RunCompareCase, RunCompareRun, RunCompareSide, ScorecardRun } from "../lib/api";
import type { EvaluationRunResultRow } from "../lib/evaluation";
import {
  agentKeyOf,
  assessCases,
  basisEvaluators,
  caseColumns,
  caseCounts,
  caseDelta,
  caseScore,
  classify,
  COMPOSITE_BASIS,
  delta,
  deltaTone,
  filterCases,
  notices,
  oneSided,
  previousComparable,
  regressedSessions,
  runComposite,
  runScore,
  sharedEvaluators,
  sideScore,
  sortCases,
} from "./compare";

const C = "Builtin.Correctness";
const H = "Builtin.Helpfulness";
const R = "Builtin.Refusal"; // a penalty evaluator: raw 1.0 = refused = bad

function rec(evaluator_id: string, score: number | null, extra: Partial<EvaluationRunResultRow> = {}): EvaluationRunResultRow {
  return { evaluator_id, level: null, score, label: null, explanation: null, error_type: null, error_message: null, ...extra };
}

function side(session_id: string, results: EvaluationRunResultRow[]): RunCompareSide {
  return { session_id, results };
}

function kase(scenario_id: string, a: RunCompareSide | null, b: RunCompareSide | null): RunCompareCase {
  return { scenario_id, input: `q ${scenario_id}`, a, b };
}

const BENCH: RunBenchmark = { kind: "dataset", key: "ds:d1", label: "hr-qa", dataset_id: "d1", revision: "f1", size: 3 };

function run(id: string, over: Partial<RunCompareRun> = {}): RunCompareRun {
  return {
    id,
    agent_id: "ag1",
    agent_name: "hr",
    dataset_id: "d1",
    dataset_name: "hr-qa",
    mode: "evaluators",
    evaluators: [C, H],
    status: "completed",
    queue_position: null,
    scores: [],
    insights: {},
    session_ids: [],
    error: null,
    benchmark: BENCH,
    ...over,
  };
}

function compare(over: Partial<RunCompare> = {}): RunCompare {
  return {
    runs: { a: run("ra"), b: run("rb") },
    alignment: "scenario",
    alignment_reason: null,
    cases: [kase("s1", side("a1", []), side("b1", []))],
    results: { a: { available: true }, b: { available: true } },
    config: null,
    ...over,
  };
}

describe("evaluators", () => {
  it("shares the evaluators both runs used, in A's order", () => {
    expect(sharedEvaluators({ evaluators: [H, C, R] }, { evaluators: [C, H] })).toEqual([H, C]);
    expect(sharedEvaluators({ evaluators: [C, C] }, { evaluators: [C] })).toEqual([C]);
  });

  it("lists A-only then B-only evaluators as one-sided", () => {
    expect(oneSided({ evaluators: [C, R] }, { evaluators: [C, H] })).toEqual([R, H]);
    expect(oneSided({ evaluators: [C] }, { evaluators: [C] })).toEqual([]);
  });
});

describe("run scores", () => {
  it("normalizes a penalty evaluator's run mean", () => {
    const r = { scores: [{ evaluatorId: R, score: 0.2 }, { evaluatorId: C, score: 0.8 }] };
    expect(runScore(r, R)).toBeCloseTo(0.8);
    expect(runScore(r, C)).toBeCloseTo(0.8);
    expect(runScore(r, H)).toBeNull();
  });

  it("averages the composite over evaluators both runs scored", () => {
    const a = { evaluators: [C, H, R], scores: [{ evaluatorId: C, score: 0.5 }, { evaluatorId: H, score: 0.7 }] };
    const b = { evaluators: [C, H], scores: [{ evaluatorId: C, score: 0.9 }, { evaluatorId: H, score: 0.9 }] };
    const out = runComposite(a, b);
    expect(out.evaluators).toEqual([C, H]);
    expect(out.a).toBeCloseTo(0.6);
    expect(out.b).toBeCloseTo(0.9);
    // a shared evaluator one side never scored drops out of both sides
    const c = { evaluators: [C, H], scores: [{ evaluatorId: C, score: 0.4 }] };
    expect(runComposite(c, b)).toEqual({ a: 0.4, b: 0.9, evaluators: [C] });
    expect(runComposite({ evaluators: [], scores: [] }, b)).toEqual({ a: null, b: null, evaluators: [] });
  });

  it("deltas are B − A and null without both", () => {
    expect(delta(0.5, 0.75)).toBeCloseTo(0.25);
    expect(delta(null, 0.5)).toBeNull();
    expect(delta(0.5, null)).toBeNull();
  });

  it("reads changes under 0.02 as flat, both ways", () => {
    expect(deltaTone(0)).toBe("flat");
    expect(deltaTone(0.019)).toBe("flat");
    expect(deltaTone(-0.019)).toBe("flat");
    expect(deltaTone(0.72 - 0.7)).toBe("up");
    expect(deltaTone(0.7 - 0.72)).toBe("down");
    expect(deltaTone(0.3)).toBe("up");
  });
});

describe("case scores", () => {
  it("averages a span-level evaluator's records and skips unscored ones", () => {
    const s = side("s", [rec(C, 1), rec(C, 0), rec(C, null, { error_type: "X" }), rec(H, 0.5)]);
    expect(sideScore(s, C)).toBeCloseTo(0.5);
    expect(sideScore(s, H)).toBeCloseTo(0.5);
    expect(sideScore(s, R)).toBeNull();
    expect(sideScore(null, C)).toBeNull();
  });

  it("flips a penalty evaluator per record", () => {
    expect(sideScore(side("s", [rec(R, 1)]), R)).toBe(0);
  });

  it("scores a side on a basis over the evaluators it has", () => {
    const s = side("s", [rec(C, 1), rec(H, 0.5)]);
    expect(caseScore(s, [C, H])).toBeCloseTo(0.75);
    expect(caseScore(s, [C, R])).toBeCloseTo(1);
    expect(caseScore(s, [R])).toBeNull();
  });

  it("maps a basis selection to its evaluators", () => {
    expect(basisEvaluators(COMPOSITE_BASIS, [C, H])).toEqual([C, H]);
    expect(basisEvaluators(H, [C, H])).toEqual([H]);
    // a stale selection falls back to the composite
    expect(basisEvaluators(R, [C, H])).toEqual([C, H]);
  });

  it("deltas a case only over evaluators both sides scored", () => {
    const a = side("a", [rec(C, 0.5)]);
    const b = side("b", [rec(C, 1), rec(H, 0)]);
    // H has no A score: the composite reads C alone on both sides
    expect(caseDelta(a, b, [C, H])).toBeCloseTo(0.5);
    expect(caseDelta(a, b, [H])).toBeNull();
    expect(caseDelta(null, b, [C])).toBeNull();
  });
});

describe("classify", () => {
  const at = (score: number, id = C) => side("s", [rec(id, score)]);

  it("counts a change of exactly 0.1 as up or down", () => {
    expect(classify(at(0.6), at(0.7), [C])).toBe("up"); // 0.7 − 0.6 = 0.0999… in floats
    expect(classify(at(0.7), at(0.6), [C])).toBe("down");
    expect(classify(at(0.2), at(0.3), [C])).toBe("up");
    expect(classify(at(0.5), at(0.59), [C])).toBe("same");
    expect(classify(at(0.59), at(0.5), [C])).toBe("same");
    expect(classify(at(0.5), at(0.5), [C])).toBe("same");
  });

  it("reads a penalty evaluator's rising raw score as a regression", () => {
    expect(classify(at(0, R), at(1, R), [R])).toBe("down");
    expect(classify(at(0.8, R), at(0.2, R), [R])).toBe("up");
  });

  it("classifies one-sided and unscored cases", () => {
    expect(classify(null, at(1), [C])).toBe("onlyB");
    expect(classify(at(1), null, [C])).toBe("onlyA");
    expect(classify(side("a", []), at(1), [C])).toBe("unscored");
    expect(classify(at(1), at(1), [])).toBe("unscored");
  });

  it("reclassifies when the basis changes", () => {
    const a = side("a", [rec(C, 1), rec(H, 0.2)]);
    const b = side("b", [rec(C, 0.5), rec(H, 0.9)]);
    expect(classify(a, b, [C])).toBe("down");
    expect(classify(a, b, [H])).toBe("up");
    expect(classify(a, b, [C, H])).toBe("up"); // mean 0.6 → 0.7
  });
});

describe("case lists", () => {
  const cases = [
    kase("same", side("a1", [rec(C, 1)]), side("b1", [rec(C, 1)])),
    kase("smallDrop", side("a2", [rec(C, 0.8)]), side("b2", [rec(C, 0.6)])),
    kase("onlyA", side("a3", [rec(C, 1)]), null),
    kase("bigDrop", side("a4", [rec(C, 1)]), side("b4", [rec(C, 0)])),
    kase("gain", side("a5", [rec(C, 0)]), side("b5", [rec(C, 0.5)])),
    kase("onlyB", null, side("b6", [rec(C, 1)])),
    kase("noScore", side("a7", []), side("b7", [rec(C, 1)])),
  ];
  const assessed = assessCases(cases, [C]);

  it("counts every class; the three change classes plus unscored make the shared count", () => {
    const counts = caseCounts(assessed);
    expect(counts).toEqual({ all: 7, shared: 5, down: 2, up: 1, same: 1, unscored: 1, onlyA: 1, onlyB: 1 });
    expect(counts.down + counts.up + counts.same + counts.unscored).toBe(counts.shared);
  });

  it("sorts regressions first (worst first), then gains, unchanged, unscored, one-sided", () => {
    expect(sortCases(assessed).map((x) => x.case.scenario_id)).toEqual([
      "bigDrop",
      "smallDrop",
      "gain",
      "same",
      "noScore",
      "onlyA",
      "onlyB",
    ]);
  });

  it("filters by class, keeping everything under all", () => {
    expect(filterCases(assessed, "all")).toHaveLength(7);
    expect(filterCases(assessed, "down").map((x) => x.case.scenario_id)).toEqual(["smallDrop", "bigDrop"]);
    expect(filterCases(assessed, "up").map((x) => x.case.scenario_id)).toEqual(["gain"]);
    expect(filterCases(assessed, "same").map((x) => x.case.scenario_id)).toEqual(["same"]);
  });

  it("collects B's regressed sessions once each", () => {
    expect(regressedSessions(assessed)).toEqual(["b2", "b4"]);
    expect(regressedSessions(assessCases(cases, [R]))).toEqual([]);
  });

  it("mirrors every class when A and B swap", () => {
    const swapped = assessCases(
      cases.map((c) => ({ ...c, a: c.b, b: c.a })),
      [C],
    );
    const mirror: Record<string, string> = { up: "down", down: "up", same: "same", unscored: "unscored", onlyA: "onlyB", onlyB: "onlyA" };
    expect(swapped.map((x) => x.cls)).toEqual(assessed.map((x) => mirror[x.cls]));
    swapped.forEach((x, i) => {
      const d = assessed[i].delta;
      if (d == null) expect(x.delta).toBeNull();
      else expect(x.delta).toBeCloseTo(-d);
    });
  });
});

describe("case columns", () => {
  const shared = ["e1", "e2", "e3", "e4", "e5", "e6"];

  it("shows the first four evaluators", () => {
    expect(caseColumns(shared, COMPOSITE_BASIS)).toEqual(["e1", "e2", "e3", "e4"]);
    expect(caseColumns(shared, "e2")).toEqual(["e1", "e2", "e3", "e4"]);
    expect(caseColumns(["e1"], COMPOSITE_BASIS)).toEqual(["e1"]);
  });

  it("puts a basis evaluator past the cap in the last column", () => {
    expect(caseColumns(shared, "e6")).toEqual(["e1", "e2", "e3", "e6"]);
    expect(caseColumns(shared, "nope")).toEqual(["e1", "e2", "e3", "e4"]);
  });
});

describe("notices", () => {
  const kinds = (cmp: RunCompare) => notices(cmp).map((n) => `${n.kind}:${n.tone}`);

  it("reports scenario alignment as comparable", () => {
    expect(kinds(compare())).toEqual(["scenario:success"]);
    expect(notices(compare())[0].shared).toBe(1);
  });

  it("reports order alignment as info", () => {
    expect(kinds(compare({ alignment: "order" }))).toEqual(["order:info"]);
  });

  it("warns when the dataset revision differs, with the shared and one-sided counts", () => {
    const cmp = compare({
      runs: { a: run("ra"), b: run("rb", { benchmark: { ...BENCH, revision: "f2" } }) },
      cases: [kase("s1", side("a1", []), side("b1", [])), kase("s2", null, side("b2", []))],
    });
    expect(kinds(cmp)).toEqual(["revision:warn"]);
    expect(notices(cmp)[0]).toMatchObject({ shared: 1, only: 1 });
    // an edited case keeps its scenario id: a new revision with every case on both sides
    const edited = compare({ runs: { a: run("ra"), b: run("rb", { benchmark: { ...BENCH, revision: "f2" } }) } });
    expect(notices(edited)).toEqual([{ kind: "revision", tone: "warn", shared: 1, only: 0 }]);
  });

  it("notes one-sided cases on the same revision", () => {
    const cmp = compare({ cases: [kase("s1", side("a1", []), side("b1", [])), kase("s2", side("a2", []), null)] });
    expect(kinds(cmp)).toEqual(["scenario:success", "partial:info"]);
  });

  it("does not warn on a revision only one side knows", () => {
    const cmp = compare({ runs: { a: run("ra", { benchmark: { ...BENCH, revision: null } }), b: run("rb") } });
    expect(kinds(cmp)).toEqual(["scenario:success"]);
  });

  it("warns that live traffic, different benchmarks and unpaired runs compare overall scores only", () => {
    const none = { alignment: "none" as const, cases: [], results: null };
    expect(kinds(compare({ ...none, alignment_reason: "live_traffic" }))).toEqual(["live:warn"]);
    expect(kinds(compare({ ...none, alignment_reason: "different_benchmark" }))).toEqual(["benchmark:warn"]);
    const unpaired = notices(compare({ ...none, alignment_reason: "session_count_mismatch" }));
    expect(unpaired).toEqual([{ kind: "unaligned", tone: "warn", reason: "session_count_mismatch" }]);
    expect(notices(compare({ ...none, alignment_reason: null }))[0].reason).toBe("unaligned");
  });

  it("adds the different-agent and one-sided-evaluator notices", () => {
    const cmp = compare({ runs: { a: run("ra", { evaluators: [C, R] }), b: run("rb", { agent_id: "ag2" }) } });
    expect(kinds(cmp)).toEqual(["scenario:success", "agents:info", "evaluators:info"]);
    expect(notices(cmp)[2].evaluators).toEqual([R, H]);
  });

  it("keys a log-source run on its service", () => {
    expect(agentKeyOf({ agent_id: "", agent_name: "svc", log_source: { service_name: "svc.a", log_group_names: [] } })).toBe(
      "logs:svc.a",
    );
    expect(agentKeyOf({ agent_id: "", agent_name: "svc" })).toBe("logs:svc");
    expect(agentKeyOf({ agent_id: "ag1", agent_name: "hr" })).toBe("ag1");
  });
});

describe("previous comparable run", () => {
  function entry(id: string, over: Partial<ScorecardRun> = {}): ScorecardRun {
    return {
      id,
      name: id,
      agent_key: "ag1",
      status: "completed",
      error: null,
      created_at: null,
      updated_at: null,
      evaluators: [C],
      scores: [{ evaluatorId: C, score: 0.5 }],
      sessions: 3,
      scope: "dataset",
      dataset_name: "hr-qa",
      dataset_version: null,
      benchmark: BENCH,
      snapshot: null,
      created_by_name: null,
      ...over,
    };
  }
  const LIVE: RunBenchmark = { kind: "live", key: "live", label: null, dataset_id: null, revision: null, size: null };

  it("picks the newest earlier completed, scored run of the same agent and benchmark", () => {
    const runs = [
      entry("old"),
      entry("newer"),
      entry("otherBench", { benchmark: { ...BENCH, key: "ds:d2" } }),
      entry("otherAgent", { agent_key: "ag2" }),
      entry("failed", { status: "failed" }),
      entry("unscored", { scores: [] }),
      entry("self"),
      entry("later"),
    ];
    expect(previousComparable(runs, "self")?.id).toBe("newer");
  });

  it("matches log-source runs and live benchmarks by key", () => {
    const runs = [
      entry("w1", { agent_key: "logs:svc", benchmark: LIVE }),
      entry("d1", { agent_key: "logs:svc" }),
      entry("w2", { agent_key: "logs:svc", benchmark: LIVE }),
    ];
    expect(previousComparable(runs, "w2")?.id).toBe("w1");
  });

  it("is null for the first run and for a run missing from the scorecard", () => {
    expect(previousComparable([entry("self"), entry("later")], "self")).toBeNull();
    expect(previousComparable([entry("a")], "missing")).toBeNull();
  });
});

// E11 (round 16, channel A) — property tests of the scorecard's composite / regression
// math (`scorecard.ts` owns it; the backend ships ledger facts only). No fast-check in the
// tree, so a seeded PRNG (mulberry32) drives N random scorecards per property; a failure
// prints its seed so the example can be replayed with `SEED=<n>`.
//
// CONTRACT (never the constants of scorecard.ts): docs/cn/interfaces/evaluation-engine.md §23,
// "Console" paragraph — "a 需要关注 panel of regressions (composite −0.05 or an evaluator −0.1)";
// "#295: a run of a versioned agent whose `snapshot` is null … counts as older than every
// numbered version (`olderVersion`)"; `runSteps` (#294, cn/r15-c): "a re-run of a version
// against that version's newest earlier run" is judge noise. The +0.05 "ok" lamp
// (`IMPROVEMENT`) has NO contract line — flagged in the N16 register (process row).
const CONTRACT_REGRESSION_COMPOSITE = 0.05; // §23: "composite −0.05"
const CONTRACT_REGRESSION_EVALUATOR = 0.1; // §23: "or an evaluator −0.1"
import { describe, expect, it } from "vitest";

import type { RunBenchmark, ScorecardRun } from "../lib/api";
import {
  IMPROVEMENT,
  REGRESSION_COMPOSITE,
  REGRESSION_EVALUATOR,
  benchmarksOf,
  comparison,
  composite,
  coreEvaluators,
  evaluatorChanges,
  fell,
  headline,
  isRegression,
  isScored,
  olderVersion,
  rose,
  runSteps,
  versionMatrix,
} from "./scorecard";

const N = Number(process.env.PROPERTY_EXAMPLES ?? 2000);
const SEED = Number(process.env.SEED ?? 20261019);

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

type Rng = () => number;
const pick = <T,>(rng: Rng, items: readonly T[]): T => items[Math.floor(rng() * items.length)];
const int = (rng: Rng, lo: number, hi: number): number => lo + Math.floor(rng() * (hi - lo + 1));
/** scores on a 0.01 grid so exact thresholds (0.05 / 0.1) are reachable */
const grid = (rng: Rng): number => int(rng, 0, 100) / 100;

const EVALUATORS = ["Builtin.Correctness", "Builtin.Helpfulness", "Builtin.GoalSuccessRate", "cust_tone"];
const BENCH: RunBenchmark = { kind: "dataset", key: "ds:d1", label: "golden", hidden: false, dataset_id: "d1", revision: "f".repeat(16), size: 3 };

function run(rng: Rng, index: number, opts: { seq?: number | null; evaluators?: string[]; status?: ScorecardRun["status"] } = {}): ScorecardRun {
  const evaluators = opts.evaluators ?? EVALUATORS.slice(0, int(rng, 1, EVALUATORS.length));
  const status = opts.status ?? pick(rng, ["completed", "completed", "completed", "failed", "stopped", "evaluating"] as const);
  const seq = opts.seq === undefined ? pick(rng, [null, 1, 2, 3]) : opts.seq;
  // a scored run may miss a score for one of its evaluators (a failed judge)
  const scores = evaluators.filter(() => rng() > 0.1).map((evaluatorId) => ({ evaluatorId, score: grid(rng) }));
  return {
    id: `r${index}`,
    name: null,
    agent_key: "agent-1",
    status,
    error: null,
    created_at: `2026-10-${String(1 + (index % 28)).padStart(2, "0")}T00:00:00+00:00`,
    updated_at: null,
    evaluators,
    scores: status === "completed" ? scores : [],
    sessions: 3,
    scope: "dataset",
    dataset_name: "golden",
    dataset_version: null,
    benchmark: BENCH,
    snapshot: seq == null ? null : { seq, source: pick(rng, ["stamped", "inferred"] as const) },
    created_by_name: null,
  };
}

function scorecard(rng: Rng): ScorecardRun[] {
  const n = int(rng, 1, 8);
  return Array.from({ length: n }, (_, i) => run(rng, i));
}

/** `for` over N seeded examples; the failing seed/index is part of the assertion message. */
function forAll(name: string, body: (rng: Rng, label: string) => void): void {
  it(name, () => {
    const root = mulberry32(SEED);
    for (let i = 0; i < N; i += 1) {
      const seed = Math.floor(root() * 2 ** 31);
      try {
        body(mulberry32(seed), `example ${i} (seed ${seed})`);
      } catch (error) {
        throw new Error(`${name} failed at example ${i}, seed ${seed}: ${(error as Error).message}`);
      }
    }
  });
}

const EPS = 1e-9;

describe("regression thresholds", () => {
  it("the module's thresholds are the contract's (§23)", () => {
    expect(REGRESSION_COMPOSITE).toBe(CONTRACT_REGRESSION_COMPOSITE);
    expect(REGRESSION_EVALUATOR).toBe(CONTRACT_REGRESSION_EVALUATOR);
  });

  forAll("fell / rose count an exact threshold and nothing smaller", (rng) => {
    const d = (int(rng, -20, 20) + pick(rng, [0, 0.4, -0.4, 0.001])) / 100;
    expect(fell(d, REGRESSION_COMPOSITE)).toBe(d - EPS <= -REGRESSION_COMPOSITE);
    expect(rose(d, IMPROVEMENT)).toBe(d + EPS >= IMPROVEMENT);
    expect(fell(null, REGRESSION_COMPOSITE)).toBe(false);
  });

  forAll("isRegression is exactly: composite fell ≥ 0.05 or an evaluator fell ≥ 0.1", (rng) => {
    const compositeDelta = rng() < 0.1 ? null : (int(rng, -30, 30) + pick(rng, [0, 0.5])) / 100;
    const changes = Array.from({ length: int(rng, 0, 4) }, (_, i) => {
      const a = grid(rng);
      const b = grid(rng);
      return { evaluatorId: EVALUATORS[i % EVALUATORS.length], a, b, delta: b - a };
    });
    const expected =
      (compositeDelta != null && compositeDelta - EPS <= -CONTRACT_REGRESSION_COMPOSITE) ||
      changes.some((c) => c.delta - EPS <= -CONTRACT_REGRESSION_EVALUATOR);
    expect(isRegression(compositeDelta, changes)).toBe(expected);
  });

  it("the boundary values themselves (§23: −0.05 composite, −0.1 evaluator; float slack)", () => {
    expect(isRegression(-0.05, [])).toBe(true);
    expect(isRegression(-0.0499, [])).toBe(false);
    expect(isRegression(0.75 - 0.8, [])).toBe(true); // float slack: 0.75 − 0.80
    expect(isRegression(0, [{ evaluatorId: "x", a: 0.7, b: 0.6, delta: 0.6 - 0.7 }])).toBe(true);
    expect(isRegression(0, [{ evaluatorId: "x", a: 0.7, b: 0.61, delta: 0.61 - 0.7 }])).toBe(false);
    expect(isRegression(null, [])).toBe(false);
  });
});

describe("run steps and the headline", () => {
  forAll("a same-version re-run is noise, never a regression; a version change applies the rule", (rng, label) => {
    const runs = scorecard(rng);
    const [bench] = benchmarksOf(runs);
    const core = coreEvaluators(bench.runs);
    const steps = runSteps(bench, core);
    const before: ScorecardRun[] = [];
    for (const r of bench.runs) {
      if (!isScored(r)) {
        expect(steps.has(r.id), label).toBe(false);
        continue;
      }
      const step = steps.get(r.id)!;
      const seq = r.snapshot?.seq;
      const same = seq == null ? null : [...before].reverse().find((p) => p.snapshot?.seq === seq) ?? null;
      expect(step.sameVersion, label).toBe(!!same);
      if (same) {
        expect(step.previous?.id, label).toBe(same.id);
        expect(step.regression, label).toBe(false); // judge noise, whatever the delta
      } else if (step.previous) {
        const expected = isRegression(step.delta, evaluatorChanges(step.previous, r));
        expect(step.regression, label).toBe(expected);
        if (seq != null) expect(olderVersion(step.previous, seq), label).toBe(true);
      } else {
        expect(step.regression, label).toBe(false);
        expect(step.delta, label).toBeNull();
      }
      before.push(r);
    }
  });

  forAll("the headline signal is `act` exactly when the regression rule fires", (rng, label) => {
    const runs = scorecard(rng);
    const [bench] = benchmarksOf(runs);
    const head = headline(bench, { agentScored: bench.scored.length, anyComparable: bench.scored.length > 1 });
    const cmp = comparison(bench.scored);
    if (!cmp || !cmp.previous) {
      expect(head.delta, label).toBeNull();
      expect(head.signal, label).toBeNull();
      return;
    }
    expect(head.previous?.id, label).toBe(cmp.previous.id);
    // the previous run is of an OLDER version (same-version re-runs are skipped)
    const seq = cmp.latest.snapshot?.seq;
    if (seq != null) expect(olderVersion(cmp.previous, seq), label).toBe(true);
    const expected = isRegression(head.delta, head.changes) ? "act" : rose(head.delta, IMPROVEMENT) ? "ok" : head.delta != null ? "info" : null;
    expect(head.signal, label).toBe(expected);
  });

  forAll("the version matrix lists each version once, newest first, every scored run counted", (rng, label) => {
    const runs = scorecard(rng);
    const [bench] = benchmarksOf(runs);
    const core = coreEvaluators(bench.runs);
    const rows = versionMatrix(bench.scored, core, EVALUATORS);
    const seqs = rows.map((r) => r.seq);
    expect([...seqs].sort((a, b) => b - a), label).toEqual(seqs);
    expect(new Set(seqs).size, label).toBe(seqs.length);
    const versioned = bench.scored.filter((r) => r.snapshot?.seq != null);
    expect(rows.reduce((sum, r) => sum + r.runs, 0), label).toBe(versioned.length);
    rows.forEach((row, i) => expect(row.older?.seq ?? null, label).toBe(rows[i + 1]?.seq ?? null));
  });
});

describe("composite basis", () => {
  // N16-obs11: `composite(run, core)` averages the core evaluators the run SCORED, so when one
  // side lacks a core score (a failed judge) the headline delta compares means over two
  // different evaluator sets — the two-run compare (`runComposite`) restricts both sides to
  // the evaluators both scored. The example below reads as a +0.4 improvement although the
  // only evaluator both runs scored is unchanged.
  it.fails("N16-obs11: a headline delta is computed over the evaluators both runs scored", () => {
    const rng = mulberry32(1);
    const previous = { ...run(rng, 0, { seq: 1, evaluators: EVALUATORS.slice(0, 2), status: "completed" }), scores: [
      { evaluatorId: EVALUATORS[0], score: 0.9 },
      { evaluatorId: EVALUATORS[1], score: 0.1 },
    ] };
    const latest = { ...run(rng, 1, { seq: 2, evaluators: EVALUATORS.slice(0, 2), status: "completed" }), scores: [
      { evaluatorId: EVALUATORS[0], score: 0.9 }, // the second judge failed: no score
    ] };
    const [bench] = benchmarksOf([previous, latest]);
    const head = headline(bench, { agentScored: 2, anyComparable: true });
    expect(head.changes.map((c) => c.delta)).toEqual([0]);
    // either no delta (reason noSharedScore) or a delta over the shared evaluator only (0)
    expect(head.delta == null || Math.abs(head.delta) < EPS).toBe(true);
  });

  forAll("composite is null without a core score and inside [0, 1] otherwise", (rng, label) => {
    const r = run(rng, 0, { status: "completed" });
    const core = coreEvaluators([r]);
    const value = composite(r, core);
    const scoredCore = core.filter((id) => r.scores.some((s) => s.evaluatorId === id));
    if (!scoredCore.length) expect(value, label).toBeNull();
    else {
      expect(value, label).not.toBeNull();
      expect(value!, label).toBeGreaterThanOrEqual(0);
      expect(value!, label).toBeLessThanOrEqual(1);
    }
  });
});

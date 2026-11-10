// Pure derivations of the agent-centric evaluation scorecard (`/v2/eval/tasks`, the
// 按 Agent tab and `?view=agent&key=`): an agent's benchmarks and its main one, the
// benchmark composite, the headline delta against the previous version (or why there is
// none), the trend's version bands and dataset-revision markers, the version × evaluator
// matrix, the series colour slots and the re-run source. Ledger rows only
// (`GET /api/eval/scorecard`); every score is read through `runScore` (normalized: higher
// is better). No DOM, so vitest covers it.
//
// Two composites exist on purpose. Compare's `runComposite` is a two-run composite over
// the evaluators both runs share. The *benchmark* composite here is over the evaluators
// every completed run of the benchmark includes (`coreEvaluators`), so one trend line
// keeps one basis while evaluators come and go.
import type {
  EvaluationScorecard,
  RunBenchmark,
  ScorecardAgent,
  ScorecardRun,
  ScorecardVersion,
  SpecChangeGroup,
} from "../lib/api";
import type { EvaluationScore } from "../lib/evaluation";
import { parseTimestamp, zonedParts } from "../lib/timestamps";
import { delta, runScore, sharedEvaluators } from "./compare";
import { combined, scoreEntry, type Significance, significance } from "./significance";
import { normalizedScore } from "./format";
import { SNAPSHOT_GROUPS } from "./pages/agents/snapshots";
import type { Signal } from "./signals";

/** A new version regresses when its composite falls at least this much… */
export const REGRESSION_COMPOSITE = 0.05;
/** …or any single evaluator falls at least this much (the compare page's case delta). */
export const REGRESSION_EVALUATOR = 0.1;
/** A rise of at least this much lights the list lamp green. */
export const IMPROVEMENT = 0.05;
/** Categorical series colours (`--v2-series-1…6`); later evaluators share a neutral ink. */
export const SERIES_SLOTS = 6;
/** Evaluator columns of the version × evaluator table: with version, runs, composite and
 *  the compare link that is 8 columns (V2 tables stay at ≤ 8). */
export const MATRIX_EVALUATORS = 4;
/** Float slack, as in compare.ts: 0.75 − 0.70 must still count as a 0.05 drop. */
const EPS = 1e-9;

const ACTIVE: ReadonlySet<string> = new Set(["queued", "invoking", "waiting", "evaluating"]);

/** Queued or still replaying / judging: a pending point on the trend, and a reason to poll. */
export function isActive(run: Pick<ScorecardRun, "status">): boolean {
  return ACTIVE.has(run.status);
}

/** Completed with at least one score — a point on the trend, a compare candidate (the same
 *  rule as compare's `previousComparable`). */
export function isScored(run: Pick<ScorecardRun, "status" | "scores">): boolean {
  return run.status === "completed" && run.scores.length > 0;
}

function mean(values: number[]): number | null {
  return values.length ? values.reduce((sum, v) => sum + v, 0) / values.length : null;
}

const present = (v: number | null): v is number => v != null;

/** `d` is a fall of at least `by` (an exact threshold counts). */
export function fell(d: number | null, by: number): boolean {
  return d != null && d - EPS <= -by;
}

/** `d` is a rise of at least `by` (an exact threshold counts). */
export function rose(d: number | null, by: number): boolean {
  return d != null && d + EPS >= by;
}

/** 平均分: a run's own mean of its normalized evaluator means (each evaluator once,
 *  unweighted); null when it has no score. */
export function runMean(run: { scores: EvaluationScore[] }): number | null {
  return mean(
    run.scores.filter((s) => typeof s.score === "number").map((s) => normalizedScore(s.score, s.evaluatorId)),
  );
}

/* ---------- benchmarks ---------- */

/** What an agent's runs were measured on: one dataset, one cloud dataset, or live traffic. */
export interface Benchmark {
  key: string;
  kind: RunBenchmark["kind"];
  /** the newest run's label (a local dataset's current name); null for live traffic
   *  and for a dataset hidden from the viewer (`hidden`, N15-obs15) */
  label: string | null;
  /** the dataset is hidden from the viewer: `benchName` renders the placeholder */
  hidden: boolean;
  /** every run, oldest first */
  runs: ScorecardRun[];
  /** the completed, scored runs, oldest first */
  scored: ScorecardRun[];
}

/**
 * An agent's runs grouped by benchmark (the input is oldest first, as the scorecard
 * lists it). Ordered by the main-benchmark rule: most scored runs first, ties to the most
 * recent scored run, then benchmarks without a scored run by their newest run. So
 * `benchmarksOf(runs)[0]` is the main benchmark.
 */
export function benchmarksOf(runs: ScorecardRun[]): Benchmark[] {
  const by = new Map<string, Benchmark>();
  const lastScored = new Map<string, number>();
  const lastAny = new Map<string, number>();
  runs.forEach((run, index) => {
    const key = run.benchmark.key;
    let bench = by.get(key);
    if (!bench) {
      bench = { key, kind: run.benchmark.kind, label: run.benchmark.label, hidden: !!run.benchmark.hidden, runs: [], scored: [] };
      by.set(key, bench);
    }
    bench.runs.push(run);
    if (run.benchmark.label) bench.label = run.benchmark.label;
    if (run.benchmark.hidden) bench.hidden = true;
    lastAny.set(key, index);
    if (isScored(run)) {
      bench.scored.push(run);
      lastScored.set(key, index);
    }
  });
  return [...by.values()].sort(
    (a, b) =>
      b.scored.length - a.scored.length ||
      (lastScored.get(b.key) ?? -1) - (lastScored.get(a.key) ?? -1) ||
      (lastAny.get(b.key) ?? -1) - (lastAny.get(a.key) ?? -1),
  );
}

/** The composite's basis: the evaluators every scored run of the benchmark includes, in
 *  the oldest scored run's order. Empty when there is no scored run. */
export function coreEvaluators(runs: ScorecardRun[]): string[] {
  const scored = runs.filter(isScored);
  if (!scored.length) return [];
  return [...new Set(scored[0].evaluators)].filter((id) => scored.every((run) => run.evaluators.includes(id)));
}

/** Every evaluator any of the runs used, in first-use order. */
export function benchEvaluators(runs: ScorecardRun[]): string[] {
  const out: string[] = [];
  for (const run of runs) for (const id of run.evaluators) if (!out.includes(id)) out.push(id);
  return out;
}

/** A scored run's benchmark composite: the mean of its normalized means over the `core`
 *  evaluators it scored; null for a run without scores or without any core score. */
export function composite(run: ScorecardRun, core: string[]): number | null {
  if (!isScored(run)) return null;
  return mean(core.map((id) => runScore(run, id)).filter(present));
}

/* ---------- headline: the latest run against the previous version ---------- */

/** The run a benchmark's latest scored run is measured against. With a version, that is
 *  the newest scored run of an *older* version (same-version re-runs are skipped); a run
 *  without a version (a log source) is measured against the scored run before it.
 *
 *  An agent's run without a version before one with a version predates the version
 *  records (the server leaves it unversioned only when the agent was deployed again after
 *  it), so it counts as an older version — see `olderVersion`. */
export interface Comparison {
  latest: ScorecardRun;
  previous: ScorecardRun | null;
  byVersion: boolean;
}

export function comparison(scored: ScorecardRun[]): Comparison | null {
  const latest = scored[scored.length - 1];
  if (!latest) return null;
  const seq = latest.snapshot?.seq;
  if (seq == null) return { latest, previous: scored[scored.length - 2] ?? null, byVersion: false };
  for (let i = scored.length - 2; i >= 0; i -= 1) {
    if (olderVersion(scored[i], seq)) return { latest, previous: scored[i], byVersion: true };
  }
  return { latest, previous: null, byVersion: true };
}

/** `run` evaluated a version older than `seq`: a lower snapshot, or none recorded. */
export function olderVersion(run: Pick<ScorecardRun, "snapshot">, seq: number): boolean {
  return run.snapshot == null || run.snapshot.seq < seq;
}

/** The agent records versions (a platform agent with a deployed snapshot): a run of it
 *  without a version predates the records, rather than having no version at all. */
export function recordsVersions(agent: Pick<ScorecardAgent, "log_source" | "versions"> | null): boolean {
  return !!agent && !agent.log_source && agent.versions.length > 0;
}

export interface EvaluatorChange {
  evaluatorId: string;
  a: number;
  b: number;
  delta: number;
}

/** B − A per evaluator both runs used and scored, largest fall first (ties keep A's order). */
export function evaluatorChanges(a: ScorecardRun, b: ScorecardRun): EvaluatorChange[] {
  const out: EvaluatorChange[] = [];
  for (const id of sharedEvaluators(a, b)) {
    const sa = runScore(a, id);
    const sb = runScore(b, id);
    if (sa != null && sb != null) out.push({ evaluatorId: id, a: sa, b: sb, delta: sb - sa });
  }
  return out.map((c, i) => ({ c, i })).sort((p, q) => p.c.delta - q.c.delta || p.i - q.i).map((p) => p.c);
}

/** The regression rule: the composite fell ≥ 0.05, or any evaluator fell ≥ 0.1. */
export function isRegression(compositeDelta: number | null, changes: EvaluatorChange[]): boolean {
  return fell(compositeDelta, REGRESSION_COMPOSITE) || changes.some((c) => fell(c.delta, REGRESSION_EVALUATOR));
}

/** Why a headline has no delta (`noSharedScore`: the pair exists, but a side has no score
 *  on the composite's evaluators, e.g. the benchmark's runs share none). */
export type NoDeltaReason = "noCompleted" | "single" | "differentDatasets" | "noEarlierVersion" | "noSharedScore";

export interface Headline {
  /** the benchmark's newest scored run */
  latest: ScorecardRun | null;
  previous: ScorecardRun | null;
  byVersion: boolean;
  core: string[];
  score: number | null;
  /** latest − previous composite; null with a `reason` */
  delta: number | null;
  changes: EvaluatorChange[];
  reason: NoDeltaReason | null;
  /** act = a regression, ok = up ≥ 0.05, info = a smaller move; null without a delta */
  signal: Signal | null;
  /** EVAL-TRUST 3.2: whether the move is real — significant when any core evaluator's
   *  change is, not significant when none is; null without a delta */
  significance: Significance | null;
}

/**
 * A benchmark's latest composite and its change against the previous version.
 * `agentScored` counts the agent's scored runs on every benchmark and `anyComparable`
 * says one benchmark has two or more: a lone run on this benchmark then reads as "only
 * one run" (more exist elsewhere) or "the runs used different datasets" (none can pair).
 */
export function headline(
  bench: Benchmark | null,
  context: { agentScored: number; anyComparable: boolean },
): Headline {
  const core = bench ? coreEvaluators(bench.runs) : [];
  const cmp = bench ? comparison(bench.scored) : null;
  const none = { previous: null, byVersion: false, delta: null, changes: [], signal: null, significance: null };
  if (!bench || !cmp) return { ...none, latest: null, core, score: null, reason: "noCompleted" };
  const score = composite(cmp.latest, core);
  if (!cmp.previous) {
    const reason: NoDeltaReason =
      bench.scored.length === 1
        ? context.agentScored > 1 && !context.anyComparable
          ? "differentDatasets"
          : "single"
        : "noEarlierVersion";
    return { ...none, latest: cmp.latest, byVersion: cmp.byVersion, core, score, reason };
  }
  const d = delta(composite(cmp.previous, core), score);
  const changes = evaluatorChanges(cmp.previous, cmp.latest);
  const signal: Signal | null = isRegression(d, changes) ? "act" : rose(d, IMPROVEMENT) ? "ok" : d != null ? "info" : null;
  const reason: NoDeltaReason | null = d == null ? "noSharedScore" : null;
  const prev = cmp.previous;
  const sig = d == null ? null : combined(core.map((id) => significance(scoreEntry(prev, id), scoreEntry(cmp.latest, id))));
  return { latest: cmp.latest, previous: cmp.previous, byVersion: cmp.byVersion, core, score, delta: d, changes, reason, signal, significance: sig };
}

/* ---------- run by run (the architect's step 2) ---------- */

/** One scored run of a benchmark against the run it follows. */
export interface RunStep {
  score: number | null;
  /** the run it is measured against; null when there is none */
  previous: ScorecardRun | null;
  /** score − previous's composite; null without a previous or a shared score */
  delta: number | null;
  /** `previous` is an earlier run of the same version: the delta is judge noise */
  sameVersion: boolean;
  /** a version change by the regression rule (composite −0.05, or an evaluator −0.1) */
  regression: boolean;
}

function newest(runs: ScorecardRun[], test: (run: ScorecardRun) => boolean): ScorecardRun | null {
  for (let i = runs.length - 1; i >= 0; i -= 1) if (test(runs[i])) return runs[i];
  return null;
}

/**
 * Each scored run of `bench` (oldest first) against the run before it: a re-run of a
 * version against that version's newest earlier run, else — as the headline — the newest
 * scored run of an older version (one without a recorded version included); a run
 * without a version against the scored run before it. Unscored runs (failed, stopped,
 * still running) get no step.
 */
export function runSteps(bench: Benchmark, core: string[]): Map<string, RunStep> {
  const out = new Map<string, RunStep>();
  const before: ScorecardRun[] = [];
  for (const run of bench.runs) {
    if (!isScored(run)) continue;
    const seq = run.snapshot?.seq;
    const same = seq == null ? null : newest(before, (r) => r.snapshot?.seq === seq);
    const previous =
      seq == null
        ? (before[before.length - 1] ?? null)
        : (same ?? newest(before, (r) => olderVersion(r, seq)));
    const score = composite(run, core);
    const d = previous ? delta(composite(previous, core), score) : null;
    const regression = !!previous && !same && isRegression(d, evaluatorChanges(previous, run));
    out.set(run.id, { score, previous, delta: d, sameVersion: !!same, regression });
    before.push(run);
  }
  return out;
}

/* ---------- the agent list ---------- */

export interface AgentRow {
  agent: ScorecardAgent;
  /** the agent's runs, oldest first */
  runs: ScorecardRun[];
  /** main benchmark first */
  benches: Benchmark[];
  main: Benchmark | null;
  head: Headline;
  /** the main benchmark's composite per scored run, oldest first */
  spark: number[];
  /** distinct agent versions the runs evaluated */
  versions: number;
  /** the newest run of any status (最近评估) */
  newest: ScorecardRun | null;
}

export function agentRow(agent: ScorecardAgent, runs: ScorecardRun[]): AgentRow {
  const benches = benchmarksOf(runs);
  const main = benches[0] ?? null;
  const agentScored = benches.reduce((sum, b) => sum + b.scored.length, 0);
  const head = headline(main, { agentScored, anyComparable: benches.some((b) => b.scored.length > 1) });
  const spark = main ? main.scored.map((run) => composite(run, head.core)).filter(present) : [];
  const versions = new Set(runs.map((run) => run.snapshot?.seq).filter((seq): seq is number => seq != null)).size;
  return { agent, runs, benches, main, head, spark, versions, newest: runs[runs.length - 1] ?? null };
}

/** The runs of each agent, keyed by `agent_key`, oldest first. */
export function runsByAgent(runs: ScorecardRun[]): Map<string, ScorecardRun[]> {
  const by = new Map<string, ScorecardRun[]>();
  for (const run of runs) {
    const list = by.get(run.agent_key);
    if (list) list.push(run);
    else by.set(run.agent_key, [run]);
  }
  return by;
}

/** One row per agent that has a run on the scorecard, most recently evaluated first. */
export function agentRows(sc: EvaluationScorecard): AgentRow[] {
  const by = runsByAgent(sc.runs);
  const order = new Map(sc.runs.map((run, i) => [run.id, i]));
  return sc.agents
    .filter((agent) => (by.get(agent.key) ?? []).length > 0)
    .map((agent) => agentRow(agent, by.get(agent.key) ?? []))
    .sort((a, b) => (order.get(b.newest?.id ?? "") ?? -1) - (order.get(a.newest?.id ?? "") ?? -1));
}

/* ---------- the trend: version bands, revision markers, series ---------- */

export interface Band {
  /** null for runs without a version (a log source): drawn without a band */
  seq: number | null;
  /** every run of the band has an inferred / observed version (never stamped) */
  approx: boolean;
  /** run indexes, inclusive */
  start: number;
  end: number;
  /** what changed since the previous band's version (diff groups, display order) */
  changes: SpecChangeGroup[];
}

/** The diff groups of the versions after `from` up to and including `to` (each entry is
 *  against the deployed snapshot before it), in display order; unknown groups as other. */
export function changesBetween(versions: ScorecardVersion[], from: number, to: number): SpecChangeGroup[] {
  const seen = new Set<string>();
  for (const v of versions) if (v.seq > from && v.seq <= to) for (const g of v.changes ?? []) seen.add(g);
  const known = SNAPSHOT_GROUPS as string[];
  return SNAPSHOT_GROUPS.filter((g) => seen.has(g) || (g === "other" && [...seen].some((s) => !known.includes(s))));
}

/** Consecutive runs of one version form a band; a band after another one lists what
 *  changed in between (`versions[].changes`, present on an `agent_id` scorecard). */
export function versionBands(runs: ScorecardRun[], versions: ScorecardVersion[] = []): Band[] {
  const bands: Band[] = [];
  runs.forEach((run, index) => {
    const seq = run.snapshot?.seq ?? null;
    const stamped = run.snapshot?.source === "stamped";
    const last = bands[bands.length - 1];
    if (last && last.seq === seq) {
      last.end = index;
      if (stamped) last.approx = false;
      return;
    }
    bands.push({ seq, approx: seq != null && !stamped, start: index, end: index, changes: [] });
  });
  let previous: number | null = null;
  for (const band of bands) {
    if (band.seq == null) continue;
    if (previous != null && band.seq > previous) band.changes = changesBetween(versions, previous, band.seq);
    previous = band.seq;
  }
  return bands;
}

export interface RevisionMarker {
  /** the first run on the new revision (the marker sits on its left edge) */
  index: number;
  fromSize: number | null;
  toSize: number | null;
}

/** Where the replayed dataset's content changed: a run whose revision differs from the
 *  last run that recorded one (runs without a revision, legacy rows, are skipped). */
export function revisionMarkers(runs: ScorecardRun[]): RevisionMarker[] {
  const out: RevisionMarker[] = [];
  let last: ScorecardRun | null = null;
  runs.forEach((run, index) => {
    const revision = run.benchmark.revision;
    if (revision == null) return;
    if (last && last.benchmark.revision !== revision) {
      out.push({ index, fromSize: last.benchmark.size, toSize: run.benchmark.size });
    }
    last = run;
  });
  return out;
}

/** Colour slot per evaluator id: the ids sorted, the first `SERIES_SLOTS` get slots 0…5
 *  and the rest null (neutral ink). Built from every evaluator on the page, so hiding a
 *  series or switching benchmark never repaints another one. */
export function seriesSlots(ids: string[]): Map<string, number | null> {
  return new Map([...new Set(ids)].sort().map((id, i) => [id, i < SERIES_SLOTS ? i : null]));
}

/** The CSS colour of a slot (custom properties in v2.css). */
export function seriesColor(slot: number | null | undefined): string {
  return slot == null ? "var(--v2-series-extra)" : `var(--v2-series-${slot + 1})`;
}

/** The value of the nearest earlier point that has one (a tooltip's "vs previous"). */
export function previousValue(values: (number | null)[], index: number): number | null {
  for (let i = index - 1; i >= 0; i -= 1) if (values[i] != null) return values[i];
  return null;
}

/* ---------- version × evaluator ---------- */

export interface MatrixCell {
  value: number | null;
  /** against the next older version row; null on the oldest row or a missing side */
  delta: number | null;
}

export interface MatrixRow {
  seq: number;
  /** every run of this version has an inferred / observed version */
  approx: boolean;
  runs: number;
  /** the version's newest scored run (the compare link's side) */
  latest: ScorecardRun;
  /** aligned to the columns */
  cells: MatrixCell[];
  composite: MatrixCell;
  /** the next older version row */
  older: { seq: number; latest: ScorecardRun } | null;
}

/** The table's evaluator columns: the composite's basis first, then the others, at most
 *  `cap`; every evaluator stays on the trend. */
export function matrixColumns(evaluators: string[], core: string[], cap = MATRIX_EVALUATORS): string[] {
  return [...core.filter((id) => evaluators.includes(id)), ...evaluators.filter((id) => !core.includes(id))].slice(0, cap);
}

/**
 * One row per version among a benchmark's scored runs, newest version first: the mean
 * per evaluator and of the run composites over that version's runs, each with its change
 * against the next older row. With one run per version the deltas equal the trend
 * tooltip's point-to-point deltas.
 */
export function versionMatrix(scored: ScorecardRun[], core: string[], columns: string[]): MatrixRow[] {
  const by = new Map<number, ScorecardRun[]>();
  for (const run of scored) {
    const seq = run.snapshot?.seq;
    if (seq == null || !isScored(run)) continue;
    const list = by.get(seq);
    if (list) list.push(run);
    else by.set(seq, [run]);
  }
  const seqs = [...by.keys()].sort((a, b) => b - a);
  const values = seqs.map((seq) => {
    const runs = by.get(seq) ?? [];
    return {
      cells: columns.map((id) => mean(runs.map((run) => runScore(run, id)).filter(present))),
      composite: mean(runs.map((run) => composite(run, core)).filter(present)),
    };
  });
  return seqs.map((seq, i) => {
    const runs = by.get(seq) ?? [];
    const olderSeq = seqs[i + 1];
    const older = olderSeq == null ? null : values[i + 1];
    const olderRuns = olderSeq == null ? [] : (by.get(olderSeq) ?? []);
    return {
      seq,
      approx: runs.every((run) => run.snapshot?.source !== "stamped"),
      runs: runs.length,
      latest: runs[runs.length - 1],
      cells: values[i].cells.map((value, c) => ({ value, delta: older ? delta(older.cells[c], value) : null })),
      composite: { value: values[i].composite, delta: older ? delta(older.composite, values[i].composite) : null },
      older: olderSeq == null ? null : { seq: olderSeq, latest: olderRuns[olderRuns.length - 1] },
    };
  });
}

/* ---------- picks, re-run ---------- */

/** Tick / untick a run for compare; at most two stay picked (a third drops the older
 *  pick), returned in run order so A is the earlier run. */
export function togglePick(picked: string[], runId: string, order: string[]): string[] {
  const next = picked.includes(runId) ? picked.filter((id) => id !== runId) : [...picked, runId].slice(-2);
  return next.sort((a, b) => order.indexOf(a) - order.indexOf(b));
}

/** N of a `window:Nh` scope; null otherwise. */
export function windowHours(datasetName: string | null | undefined): number | null {
  const m = /^window:(\d+)h$/.exec(datasetName ?? "");
  return m ? Number(m[1]) : null;
}

/** 再评一次 applies to a dataset replay (local or cloud) or a time window: re-scoring the
 *  same chosen sessions or log streams would only measure judge noise. */
export function rerunnable(run: Pick<ScorecardRun, "scope" | "dataset_name">): boolean {
  return run.scope === "dataset" || run.scope === "cloud" || (run.scope === "window" && windowHours(run.dataset_name) != null);
}

/** Why 再评一次 is off: the agent is deleted, the benchmark has no completed run, or none
 *  of its completed runs has a scope that can run again. */
export type RerunBlock = "deleted" | "noRun" | "unsupported";

/** The run 再评一次 copies: the benchmark's newest completed run that can run again. */
export function rerunSource(
  bench: Benchmark | null,
  agent: Pick<ScorecardAgent, "deleted">,
): { run: ScorecardRun | null; block: RerunBlock | null } {
  const completed = (bench?.runs ?? []).filter((run) => run.status === "completed");
  const run = [...completed].reverse().find(rerunnable) ?? null;
  if (agent.deleted) return { run, block: "deleted" };
  if (!completed.length) return { run: null, block: "noRun" };
  return run ? { run, block: null } : { run: null, block: "unsupported" };
}

/* ---------- short times (display zone, naive ledger times read as UTC) ---------- */

const pad = (n: number) => String(n).padStart(2, "0");

/** "10-16"; "—" for a missing or unreadable value. */
export function shortDate(value: string | null | undefined): string {
  const d = parseTimestamp(value);
  if (!d) return "—";
  const z = zonedParts(d); // N17-obs463: the display zone, not the browser's
  return `${pad(z.month)}-${pad(z.day)}`;
}

/** "14:05"; "" for a missing or unreadable value. */
export function shortTime(value: string | null | undefined): string {
  const d = parseTimestamp(value);
  if (!d) return "";
  const z = zonedParts(d);
  return `${pad(z.hour)}:${pad(z.minute)}`;
}

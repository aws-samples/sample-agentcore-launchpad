// Pure logic of the two-run compare page (`/v2/eval/tasks?view=compare&a=&b=`): the
// evaluators both runs share, a run's and a case's score on a basis (the composite or
// one evaluator), how a case moved from A to B, the counts and table order, which
// comparability notices apply, and the "compare with the previous run" target. Every
// score is compared normalized (`normalizedScore`: higher is better), so a penalty
// evaluator's falling raw score reads as a gain. No DOM, so vitest covers it.
import type {
  RunCompare,
  RunCompareAlignmentReason,
  RunCompareCase,
  RunCompareSide,
  ScorecardRun,
} from "../lib/api";
import type { EvaluationScore, LogSource } from "../lib/evaluation";
import { normalizedScore } from "./format";

/** A case improved / regressed when its normalized score moved by at least this. */
export const CASE_DELTA = 0.1;
/** A delta chip reads "flat" (gray) when the change is smaller than this. */
export const FLAT_DELTA = 0.02;
/** Float slack: 0.7 − 0.6 is 0.0999…, and a change of exactly 0.1 must still count. */
const EPS = 1e-9;
/** Evaluator columns of the case table. With the expander, the case and the change tag
 *  that makes 7 columns (V2 tables stay at ≤ 8). */
export const CASE_COLUMNS = 4;
/** The basis selector's value for the composite; any other value is one evaluator id. */
export const COMPOSITE_BASIS = "";

type Evaluated = { evaluators: string[] };
type Scored = { scores: EvaluationScore[] };

function mean(values: number[]): number | null {
  return values.length ? values.reduce((sum, v) => sum + v, 0) / values.length : null;
}

/** The evaluators both runs used, in A's order. */
export function sharedEvaluators(a: Evaluated, b: Evaluated): string[] {
  const inB = new Set(b.evaluators);
  return [...new Set(a.evaluators)].filter((id) => inB.has(id));
}

/** The evaluators only one run used (A's, then B's). They take no part in the compare. */
export function oneSided(a: Evaluated, b: Evaluated): string[] {
  const inA = new Set(a.evaluators);
  const inB = new Set(b.evaluators);
  return [...new Set([...a.evaluators.filter((id) => !inB.has(id)), ...b.evaluators.filter((id) => !inA.has(id))])];
}

/** A run's normalized mean for one evaluator, from the run row; null when it has none. */
export function runScore(run: Scored, evaluatorId: string): number | null {
  const hit = run.scores.find((s) => s.evaluatorId === evaluatorId && typeof s.score === "number");
  return hit ? normalizedScore(hit.score, evaluatorId) : null;
}

/**
 * The composite of two runs: the mean of each run's normalized means over the shared
 * evaluators that both runs scored, so both sides average the same set. This is the
 * two-run case of the benchmark rule ("the evaluators every compared run includes").
 */
export function runComposite(
  a: Evaluated & Scored,
  b: Evaluated & Scored,
): { a: number | null; b: number | null; evaluators: string[] } {
  const evaluators = sharedEvaluators(a, b).filter((id) => runScore(a, id) != null && runScore(b, id) != null);
  const of = (run: Scored) => mean(evaluators.map((id) => runScore(run, id) as number));
  return { a: of(a), b: of(b), evaluators };
}

/** B − A; null when either side is missing. */
export function delta(a: number | null, b: number | null): number | null {
  return a == null || b == null ? null : b - a;
}

export type DeltaTone = "up" | "down" | "flat";

/** A delta chip's tone: flat inside ±FLAT_DELTA, else its direction. */
export function deltaTone(value: number): DeltaTone {
  if (Math.abs(value) + EPS < FLAT_DELTA) return "flat";
  return value > 0 ? "up" : "down";
}

/** One side's normalized score for one evaluator on a case: the mean over its records (a
 *  span-level evaluator writes one per tool call); null when no record carries a score. */
export function sideScore(side: RunCompareSide | null, evaluatorId: string): number | null {
  if (!side) return null;
  return mean(
    side.results
      .filter((r) => r.evaluator_id === evaluatorId && typeof r.score === "number")
      .map((r) => normalizedScore(r.score as number, evaluatorId)),
  );
}

/** The evaluators a basis selection covers: every shared one for the composite, else the
 *  one chosen. A selection that is no longer shared falls back to the composite. */
export function basisEvaluators(selection: string, shared: string[]): string[] {
  return selection !== COMPOSITE_BASIS && shared.includes(selection) ? [selection] : shared;
}

/** A side's score on a basis: the mean of its per-evaluator scores over the basis
 *  evaluators it scored; null when it scored none. */
export function caseScore(side: RunCompareSide | null, basis: string[]): number | null {
  return mean(basis.map((id) => sideScore(side, id)).filter((v): v is number => v != null));
}

/** B − A of one case on a basis, over the basis evaluators that BOTH sides scored, so the
 *  two means cover the same evaluators. Null when a side is missing or none qualifies. */
export function caseDelta(a: RunCompareSide | null, b: RunCompareSide | null, basis: string[]): number | null {
  if (!a || !b) return null;
  const both = basis.filter((id) => sideScore(a, id) != null && sideScore(b, id) != null);
  return delta(caseScore(a, both), caseScore(b, both));
}

/**
 * How a case moved from A to B:
 * - `up` / `down`: the normalized change on the basis is at least CASE_DELTA (an exact 0.1
 *   counts);
 * - `same`: a smaller change;
 * - `unscored`: both runs played it, but no basis evaluator scored it on both sides (a
 *   failed judge, a session the batch skipped, a truncated results read);
 * - `onlyA` / `onlyB`: only one run has a session for it.
 */
export type CaseClass = "down" | "up" | "same" | "unscored" | "onlyA" | "onlyB";

export function classify(a: RunCompareSide | null, b: RunCompareSide | null, basis: string[]): CaseClass {
  if (!a) return b ? "onlyB" : "unscored";
  if (!b) return "onlyA";
  const d = caseDelta(a, b, basis);
  if (d == null) return "unscored";
  if (d + EPS >= CASE_DELTA) return "up";
  if (d - EPS <= -CASE_DELTA) return "down";
  return "same";
}

export interface AssessedCase {
  case: RunCompareCase;
  cls: CaseClass;
  /** B − A on the basis; null for a one-sided or unscored case */
  delta: number | null;
}

/** Every case classified on one basis, in the server's order (B's scenario order). */
export function assessCases(cases: RunCompareCase[], basis: string[]): AssessedCase[] {
  return cases.map((c) => ({ case: c, cls: classify(c.a, c.b, basis), delta: caseDelta(c.a, c.b, basis) }));
}

export type CaseCounts = Record<CaseClass, number> & {
  all: number;
  /** cases both runs played: down + up + same + unscored */
  shared: number;
};

export function caseCounts(assessed: AssessedCase[]): CaseCounts {
  const counts: CaseCounts = { all: assessed.length, shared: 0, down: 0, up: 0, same: 0, unscored: 0, onlyA: 0, onlyB: 0 };
  for (const item of assessed) {
    counts[item.cls] += 1;
    if (item.cls !== "onlyA" && item.cls !== "onlyB") counts.shared += 1;
  }
  return counts;
}

const RANK: Record<CaseClass, number> = { down: 0, up: 1, same: 2, unscored: 3, onlyA: 4, onlyB: 5 };

/** Table order: regressions (largest drop first), improvements (largest gain first),
 *  unchanged, unscored, then the one-sided cases. Ties keep the server's order. */
export function sortCases(assessed: AssessedCase[]): AssessedCase[] {
  return assessed
    .map((item, index) => ({ item, index }))
    .sort((p, q) => {
      const rank = RANK[p.item.cls] - RANK[q.item.cls];
      if (rank) return rank;
      if (p.item.cls === "down" || p.item.cls === "up") {
        const size = Math.abs(q.item.delta ?? 0) - Math.abs(p.item.delta ?? 0);
        if (size) return size;
      }
      return p.index - q.index;
    })
    .map((p) => p.item);
}

/** The table filter. `all` also lists the unscored and one-sided cases. */
export type CaseFilter = "all" | "down" | "up" | "same";

export function filterCases(assessed: AssessedCase[], filter: CaseFilter): AssessedCase[] {
  return filter === "all" ? assessed : assessed.filter((item) => item.cls === filter);
}

/** B's sessions of the regressed cases, for "regressed cases → dataset". */
export function regressedSessions(assessed: AssessedCase[]): string[] {
  const out: string[] = [];
  for (const item of assessed) {
    const sid = item.cls === "down" ? item.case.b?.session_id : undefined;
    if (sid && !out.includes(sid)) out.push(sid);
  }
  return out;
}

/** The case table's evaluator columns: the first CASE_COLUMNS shared evaluators. A
 *  single-evaluator basis outside them takes the last column, so the evaluator the
 *  classification reads is always on screen. */
export function caseColumns(shared: string[], selection: string, cap = CASE_COLUMNS): string[] {
  const columns = shared.slice(0, cap);
  if (selection !== COMPOSITE_BASIS && shared.includes(selection) && !columns.includes(selection)) {
    columns[columns.length - 1] = selection;
  }
  return columns;
}

/**
 * The comparability notices above the page:
 * - `scenario` (success) / `order` (info): the cases line up by scenario id, or by session
 *   order for an older run without a scenario map;
 * - `revision` (warn): the two replays used different revisions of the dataset, so only
 *   the cases both have are compared (it replaces the alignment notice);
 * - `partial` (info): the revision is the same, but some cases only one run played (a
 *   skipped persona);
 * - `live` / `benchmark` / `unaligned` (warn): overall scores only (live traffic,
 *   different benchmarks, or a legacy run that cannot be paired, with the reason);
 * - `agents` (info): the runs evaluated two different agents;
 * - `evaluators` (info): evaluators used by one run only, which take no part;
 * - `judges` (warn): the runs' built-in judges differ (P3.2), so their scores are not a
 *   like-for-like comparison.
 */
export type CompareNoticeKind =
  | "scenario"
  | "order"
  | "revision"
  | "partial"
  | "live"
  | "benchmark"
  | "unaligned"
  | "agents"
  | "evaluators"
  | "judges";

export interface CompareNotice {
  kind: CompareNoticeKind;
  tone: "success" | "info" | "warn";
  /** aligned kinds: the cases both runs played, and those only one did */
  shared?: number;
  only?: number;
  /** `unaligned`: the server's reason */
  reason?: RunCompareAlignmentReason;
  /** `evaluators`: the one-sided evaluator ids */
  evaluators?: string[];
}

/** The agent a run belongs to: its id, or `logs:<service>` for a log-source run (the
 *  backend's `agent_key`). */
export function agentKeyOf(run: { agent_id: string; agent_name: string | null; log_source?: LogSource | null }): string {
  // a log-source run has no agent to hide, so its `agent_name` is never masked (N15-obs8)
  return run.agent_id || `logs:${run.log_source?.service_name || run.agent_name || ""}`;
}

export function notices(cmp: RunCompare): CompareNotice[] {
  const { a, b } = cmp.runs;
  const out: CompareNotice[] = [];
  if (cmp.alignment === "none") {
    const reason = cmp.alignment_reason ?? "unaligned";
    if (reason === "live_traffic") out.push({ kind: "live", tone: "warn" });
    else if (reason === "different_benchmark") out.push({ kind: "benchmark", tone: "warn" });
    else out.push({ kind: "unaligned", tone: "warn", reason });
  } else {
    const shared = cmp.cases.filter((c) => c.a && c.b).length;
    const only = cmp.cases.length - shared;
    const revA = a.benchmark.revision;
    const revB = b.benchmark.revision;
    if (revA != null && revB != null && revA !== revB) {
      out.push({ kind: "revision", tone: "warn", shared, only });
    } else {
      out.push({ kind: cmp.alignment, tone: cmp.alignment === "scenario" ? "success" : "info", shared });
      if (only) out.push({ kind: "partial", tone: "info", only });
    }
  }
  if (agentKeyOf(a) !== agentKeyOf(b)) out.push({ kind: "agents", tone: "info" });
  const one = oneSided(a, b);
  if (one.length) out.push({ kind: "evaluators", tone: "info", evaluators: one });
  if (cmp.judge_mismatch) out.push({ kind: "judges", tone: "warn" });
  return out;
}

/** The run a finished run is compared with by "compare with the previous run": the newest
 *  earlier completed, scored run of the same agent on the same benchmark (the scorecard
 *  lists runs oldest first). Null when there is none, or the run is not on the scorecard. */
export function previousComparable(runs: ScorecardRun[], runId: string): ScorecardRun | null {
  const index = runs.findIndex((r) => r.id === runId);
  if (index < 0) return null;
  const self = runs[index];
  for (let i = index - 1; i >= 0; i -= 1) {
    const run = runs[i];
    if (
      run.agent_key === self.agent_key &&
      run.benchmark.key === self.benchmark.key &&
      run.status === "completed" &&
      run.scores.length > 0
    ) {
      return run;
    }
  }
  return null;
}

// The 按 Agent tab's "needs a look" queue (v2-attention-signals): regressions of a new
// agent version on a dataset benchmark, the same drop on a dataset whose content changed
// (to confirm), and recent failed runs. Pure and computed on read from the scorecard the
// list already loads; the page turns each entry into a row and owns the copy and navigation.
import type { EvaluationScorecard, ScorecardAgent, ScorecardRun } from "../../../lib/api";
import { parseTimestamp } from "../../../lib/timestamps";
import { delta } from "../../compare";
import {
  type Benchmark,
  benchmarksOf,
  comparison,
  composite,
  coreEvaluators,
  type EvaluatorChange,
  evaluatorChanges,
  fell,
  isRegression,
  REGRESSION_COMPOSITE,
  runsByAgent,
} from "../../scorecard";
import { rankBySignal, type Signal } from "../../signals";

/** Rows the panel shows (`AttentionPanel` `cap`); its header counts every entry. */
export const SCORECARD_ATTENTION_CAP = 5;
/** Longest error hint a failed-run row shows before it is cut. */
export const HINT_MAX = 120;
/** A failed run is listed this long after it was created, unless a later run fixed it. */
export const FAILED_RUN_TTL_MS = 7 * 24 * 60 * 60 * 1000;

/** A new version scored lower than the previous one on the same dataset benchmark. */
export interface RegressionEntry {
  key: string;
  /** `pending`: the two runs replayed different dataset revisions, so the drop may come
   *  from the dataset — waits for the user to confirm */
  kind: "regression" | "pending";
  signal: Signal;
  agent: ScorecardAgent;
  bench: Benchmark;
  /** the previous version's latest scored run (A) and the newest version's (B) */
  previous: ScorecardRun;
  latest: ScorecardRun;
  from: number;
  to: number;
  composite: { a: number | null; b: number | null; delta: number | null };
  /** the composite fell ≥ 0.05 (else only single evaluators fell ≥ 0.1) */
  compositeDrop: boolean;
  /** the two evaluators that fell most, largest fall first */
  worst: EvaluatorChange[];
}

export interface FailedEntry {
  key: string;
  kind: "failed";
  signal: Signal;
  agent: ScorecardAgent | null;
  run: ScorecardRun;
  /** the error's first line, clipped; "" when none */
  detail: string;
}

export type ScorecardEntry = RegressionEntry | FailedEntry;

const clip = (text: string) => (text.length > HINT_MAX ? `${text.slice(0, HINT_MAX - 1)}…` : text);
const firstLine = (text: string | null | undefined) => (text ?? "").trim().split("\n")[0].trim();

function regressionOf(agent: ScorecardAgent, bench: Benchmark): RegressionEntry | null {
  // live traffic scores different sessions each time: an overall trend, never a regression
  if (bench.kind === "live") return null;
  const cmp = comparison(bench.scored);
  // a same-version re-run never pairs (`comparison` skips it); no version, no regression
  if (!cmp?.previous || !cmp.byVersion) return null;
  const from = cmp.previous.snapshot?.seq;
  const to = cmp.latest.snapshot?.seq;
  if (from == null || to == null) return null;
  const core = coreEvaluators(bench.runs);
  const a = composite(cmp.previous, core);
  const b = composite(cmp.latest, core);
  const d = delta(a, b);
  const changes = evaluatorChanges(cmp.previous, cmp.latest);
  if (!isRegression(d, changes)) return null;
  const revA = cmp.previous.benchmark.revision;
  const revB = cmp.latest.benchmark.revision;
  const pending = revA != null && revB != null && revA !== revB;
  return {
    key: `${pending ? "pending" : "regression"}:${agent.key}:${bench.key}`,
    kind: pending ? "pending" : "regression",
    signal: pending ? "wait" : "act",
    agent,
    bench,
    previous: cmp.previous,
    latest: cmp.latest,
    from,
    to,
    composite: { a, b, delta: d },
    compositeDrop: fell(d, REGRESSION_COMPOSITE),
    worst: changes.filter((c) => c.delta < 0).slice(0, 2),
  };
}

/**
 * Every agent's regressions (act) and dataset-changed drops (wait) on its dataset and
 * cloud benchmarks — the newest version's latest scored run against the previous
 * version's — plus its failed runs (act), each kind newest first and ranked by signal; the
 * panel caps them at `SCORECARD_ATTENTION_CAP`. A deleted agent raises nothing: there is no
 * version left to fix or re-run. A failed run expires: it is listed only within
 * `FAILED_RUN_TTL_MS` of its creation (a run whose time cannot be read is kept) and until
 * a later run of the same agent on the same benchmark completes.
 */
export function scorecardAttention(sc: EvaluationScorecard, now = Date.now()): ScorecardEntry[] {
  const order = new Map(sc.runs.map((run, i) => [run.id, i]));
  const newestFirst = (a: ScorecardRun, b: ScorecardRun) => (order.get(b.id) ?? 0) - (order.get(a.id) ?? 0);
  const byAgent = runsByAgent(sc.runs);
  const agents = new Map(sc.agents.map((agent) => [agent.key, agent]));

  const drops: RegressionEntry[] = [];
  for (const agent of sc.agents) {
    if (agent.deleted) continue;
    for (const bench of benchmarksOf(byAgent.get(agent.key) ?? [])) {
      const entry = regressionOf(agent, bench);
      if (entry) drops.push(entry);
    }
  }
  drops.sort((a, b) => newestFirst(a.latest, b.latest));

  // the newest completed run per agent and benchmark (runs arrive oldest-first)
  const benchOf = (run: ScorecardRun) => `${run.agent_key}|${run.benchmark.key}`;
  const lastCompleted = new Map<string, number>();
  for (const run of sc.runs) if (run.status === "completed") lastCompleted.set(benchOf(run), order.get(run.id) ?? 0);
  const listed = (run: ScorecardRun) => {
    const at = parseTimestamp(run.created_at)?.getTime();
    if (at !== undefined && now - at > FAILED_RUN_TTL_MS) return false;
    return (lastCompleted.get(benchOf(run)) ?? -1) < (order.get(run.id) ?? 0);
  };

  const failed: FailedEntry[] = sc.runs
    .filter((run) => run.status === "failed" && !agents.get(run.agent_key)?.deleted && listed(run))
    .sort(newestFirst)
    .map((run) => ({
      key: `failed:${run.id}`,
      kind: "failed" as const,
      signal: "act" as const,
      agent: agents.get(run.agent_key) ?? null,
      run,
      detail: clip(firstLine(run.error)),
    }));

  // regressions lead the act rows, then failures; the wait rows follow (a stable rank)
  return rankBySignal<ScorecardEntry>([
    ...drops.filter((d) => d.kind === "regression"),
    ...failed,
    ...drops.filter((d) => d.kind === "pending"),
  ]);
}

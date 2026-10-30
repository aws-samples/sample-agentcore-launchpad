import type { OnlineEvalResultsRecord } from "../lib/api";
import type { EvaluationRunResults, FailureClass } from "../lib/evaluation";
import { passesBar } from "../lib/evaluators";
import { normalizedScore } from "./format";
import type { TaskSource, V2Task } from "./tasks";

/** One judged result (a session × evaluator record) as the V2 tables show it. */
export interface ResultRow {
  key: string;
  time: string | null;
  taskKey: string;
  taskName: string;
  agent: string;
  agentId: string | null;
  source: TaskSource;
  sessionId: string | null;
  traceId: string | null;
  /** N17-obs317: the call a TOOL_CALL row judged */
  spanId?: string | null;
  toolName?: string | null;
  /** the judged turn's extraction reconciliation (ok | mismatch | unverifiable) */
  extractionStatus?: string | null;
  /** the judge model that produced the row */
  judgeModelId?: string | null;
  /** N17-obs391: the dataset scenario of the session */
  scenarioId?: string | null;
  scenarioLabel?: string | null;
  evaluatorId: string;
  level: string | null;
  score: number | null;
  normalized: number | null;
  label: string | null;
  explanation: string | null;
  error: string | null;
  /** passed = normalized ≥ the evaluator's pass bar (N17-obs425: `passThreshold`); failed = a
   *  Bad Case; error = the judge failed */
  outcome: "passed" | "failed" | "error";
  /** eval accuracy item 1: the session's extraction reconciliation failed — the judges may
   *  have scored text the agent never answered; the row carries a visible flag */
  extractionMismatch?: boolean;
  /** R3-f: why the row did not score (timeout, throttling, …); null on a judged row */
  failureClass?: FailureClass | null;
}

function outcomeOf(normalized: number | null, error: string | null, evaluatorId: string): ResultRow["outcome"] {
  if (error || normalized == null) return "error";
  return passesBar(normalized, evaluatorId) ? "passed" : "failed";
}

export function rowsFromRun(task: V2Task, results: EvaluationRunResults): ResultRow[] {
  const rows: ResultRow[] = [];
  for (const session of results.sessions) {
    const extractionMismatch = session.extraction_check?.status === "mismatch";
    session.results.forEach((r, i) => {
      const normalized = r.score == null ? null : normalizedScore(r.score, r.evaluator_id);
      const error = r.error_message || r.error_type || null;
      rows.push({
        key: `${task.kind}:${task.id}:${session.session_id}:${r.evaluator_id}:${i}`,
        time: task.updatedAt,
        taskKey: `${task.kind}:${task.id}`,
        taskName: task.name,
        agent: task.agentName,
        agentId: task.agentId,
        source: task.source,
        sessionId: session.session_id,
        traceId: r.trace_id ?? null,
        spanId: r.span_id ?? null,
        toolName: r.tool_name ?? null,
        extractionStatus: r.extraction_status ?? null,
        judgeModelId: r.judge_model_id ?? null,
        scenarioId: r.scenario_id ?? session.scenario_id ?? null,
        scenarioLabel: r.scenario_label ?? session.scenario_label ?? null,
        evaluatorId: r.evaluator_id,
        level: r.level,
        score: r.score,
        normalized,
        label: r.label,
        explanation: r.explanation,
        error,
        outcome: outcomeOf(normalized, error, r.evaluator_id),
        extractionMismatch,
        failureClass: r.failure_class ?? null,
      });
    });
  }
  return rows;
}

export function rowsFromOnline(task: V2Task, records: OnlineEvalResultsRecord[]): ResultRow[] {
  return records.map((r, i) => {
    const evaluatorId = r.evaluator_id ?? "—";
    const normalized = r.score == null ? null : normalizedScore(r.score, evaluatorId);
    return {
      key: `${task.kind}:${task.id}:${r.session_id ?? ""}:${evaluatorId}:${r.time ?? ""}:${i}`,
      time: r.time,
      taskKey: `${task.kind}:${task.id}`,
      taskName: task.name,
      agent: task.agentName,
      agentId: task.agentId,
      source: task.source,
      sessionId: r.session_id,
      traceId: r.trace_id,
      spanId: r.span_id ?? null,
      toolName: r.tool_name ?? null,
      extractionStatus: r.extraction_status ?? null,
      evaluatorId,
      level: r.level,
      score: r.score,
      normalized,
      label: r.label,
      explanation: r.explanation,
      error: r.error,
      outcome: outcomeOf(normalized, r.error, evaluatorId),
    };
  });
}

export interface ResultSummary {
  total: number;
  passed: number;
  failed: number;
  errors: number;
  mean: number | null;
  byEvaluator: { evaluatorId: string; count: number; mean: number | null; failed: number }[];
}

export function summarize(rows: ResultRow[]): ResultSummary {
  const scored = rows.filter((r) => r.normalized != null);
  const byId = new Map<string, ResultRow[]>();
  for (const r of rows) byId.set(r.evaluatorId, [...(byId.get(r.evaluatorId) ?? []), r]);
  const mean = (list: ResultRow[]) => {
    const s = list.filter((r) => r.normalized != null);
    return s.length ? s.reduce((a, r) => a + (r.normalized as number), 0) / s.length : null;
  };
  return {
    total: rows.length,
    passed: rows.filter((r) => r.outcome === "passed").length,
    failed: rows.filter((r) => r.outcome === "failed").length,
    errors: rows.filter((r) => r.outcome === "error").length,
    mean: scored.length ? mean(scored) : null,
    byEvaluator: [...byId.entries()]
      .map(([evaluatorId, list]) => ({
        evaluatorId,
        count: list.length,
        mean: mean(list),
        failed: list.filter((r) => r.outcome === "failed").length,
      }))
      .sort((a, b) => (a.mean ?? 2) - (b.mean ?? 2)),
  };
}

/** Normalized-score bands of the result filters (`high` includes a perfect 1). */
export const SCORE_BANDS = {
  low: [0, 0.4],
  mid: [0.4, 0.7],
  high: [0.7, 1.0001],
} as const;
export type ScoreBand = keyof typeof SCORE_BANDS;

export interface ResultFilter {
  evaluator?: string;
  outcome?: string;
  band?: string;
  /** case-insensitive substring of label · explanation / error · session · task */
  q?: string;
}

export function filterResults(rows: ResultRow[], f: ResultFilter): ResultRow[] {
  const needle = (f.q ?? "").trim().toLowerCase();
  return rows.filter((r) => {
    if (f.evaluator && r.evaluatorId !== f.evaluator) return false;
    if (f.outcome && r.outcome !== f.outcome) return false;
    if (f.band) {
      const [lo, hi] = SCORE_BANDS[f.band as ScoreBand];
      if (r.normalized == null || r.normalized < lo || r.normalized >= hi) return false;
    }
    if (!needle) return true;
    return `${r.label ?? ""} ${r.explanation ?? ""} ${r.error ?? ""} ${r.sessionId ?? ""} ${r.taskName}`
      .toLowerCase()
      .includes(needle);
  });
}

export type ResultSortKey = "outcome" | "time" | "raw" | "norm" | "evaluator";
export interface ResultSort {
  key: ResultSortKey;
  dir: "asc" | "desc";
}

// failed first ascending: the Bad Cases are what a reader sorts to find
const OUTCOME_RANK: Record<ResultRow["outcome"], number> = { failed: 0, error: 1, passed: 2 };

function sortValue(r: ResultRow, key: ResultSortKey): string | number | null {
  switch (key) {
    case "outcome":
      return OUTCOME_RANK[r.outcome];
    case "time":
      return r.time;
    case "raw":
      return r.score;
    case "norm":
      return r.normalized;
    case "evaluator":
      return r.evaluatorId;
  }
}

/** A stable sort on one column; rows without a value stay last either way. */
export function sortResults(rows: ResultRow[], sort: ResultSort | null): ResultRow[] {
  if (!sort) return rows;
  const sign = sort.dir === "asc" ? 1 : -1;
  return rows
    .map((row, i) => ({ row, i, v: sortValue(row, sort.key) }))
    .sort((a, b) => {
      if (a.v == null || b.v == null) return a.v == null ? (b.v == null ? a.i - b.i : 1) : -1;
      const c = typeof a.v === "number" && typeof b.v === "number" ? a.v - b.v : String(a.v).localeCompare(String(b.v));
      return c === 0 ? a.i - b.i : sign * c;
    })
    .map((x) => x.row);
}

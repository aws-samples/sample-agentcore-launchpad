import type { OnlineEvalResultsRecord } from "../lib/api";
import type { EvaluationRunResults } from "../lib/evaluation";
import { BAD_CASE_THRESHOLD, normalizedScore } from "./format";
import type { TaskSource, V2Task } from "./tasks";

/** One judged result (a session × evaluator record) as the V2 tables show it. */
export interface ResultRow {
  key: string;
  time: string | null;
  taskKey: string;
  taskName: string;
  agent: string;
  source: TaskSource;
  sessionId: string | null;
  traceId: string | null;
  evaluatorId: string;
  level: string | null;
  score: number | null;
  normalized: number | null;
  label: string | null;
  explanation: string | null;
  error: string | null;
  /** passed = normalized ≥ threshold; failed = a Bad Case; error = the judge failed */
  outcome: "passed" | "failed" | "error";
}

function outcomeOf(normalized: number | null, error: string | null): ResultRow["outcome"] {
  if (error || normalized == null) return "error";
  return normalized >= BAD_CASE_THRESHOLD ? "passed" : "failed";
}

export function rowsFromRun(task: V2Task, results: EvaluationRunResults): ResultRow[] {
  const rows: ResultRow[] = [];
  for (const session of results.sessions) {
    session.results.forEach((r, i) => {
      const normalized = r.score == null ? null : normalizedScore(r.score, r.evaluator_id);
      const error = r.error_message || r.error_type || null;
      rows.push({
        key: `${task.kind}:${task.id}:${session.session_id}:${r.evaluator_id}:${i}`,
        time: task.updatedAt,
        taskKey: `${task.kind}:${task.id}`,
        taskName: task.name,
        agent: task.agentName,
        source: task.source,
        sessionId: session.session_id,
        traceId: null,
        evaluatorId: r.evaluator_id,
        level: r.level,
        score: r.score,
        normalized,
        label: r.label,
        explanation: r.explanation,
        error,
        outcome: outcomeOf(normalized, error),
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
      source: task.source,
      sessionId: r.session_id,
      traceId: r.trace_id,
      evaluatorId,
      level: r.level,
      score: r.score,
      normalized,
      label: r.label,
      explanation: r.explanation,
      error: r.error,
      outcome: outcomeOf(normalized, r.error),
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

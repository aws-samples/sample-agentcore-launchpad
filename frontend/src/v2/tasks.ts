import type { TFunction } from "i18next";

import { api, type OnlineEvalConfigRow } from "../lib/api";
import type { EvaluationRunInfo } from "../lib/evaluation";
import type { TagTone } from "./ui";

/**
 * One "evaluation task" of the V2 console. Two backend resources present as
 * tasks: a batch evaluation run (history strategy — a dataset replay, a time
 * window or chosen sessions, evaluated once) and an online evaluation config
 * (continuous strategy — sampled live sessions scored as they arrive).
 */
export type TaskKind = "run" | "online";
export type TaskSource = "dataset" | "cloud" | "window" | "sessions" | "live";
export type TaskStatus = "queued" | "running" | "completed" | "failed" | "stopped" | "paused" | "pending";

export interface V2Task {
  kind: TaskKind;
  id: string;
  name: string;
  description: string;
  agentId: string | null;
  agentName: string;
  evaluators: string[];
  source: TaskSource;
  /** dataset name / window length / sampling percentage */
  sourceDetail: string;
  status: TaskStatus;
  createdAt: string | null;
  updatedAt: string | null;
  run?: EvaluationRunInfo;
  online?: OnlineEvalConfigRow;
}

function runSource(run: EvaluationRunInfo): { source: TaskSource; detail: string } {
  const name = run.dataset_name ?? "";
  if (name.startsWith("window:")) return { source: "window", detail: name.slice("window:".length) };
  if (name.startsWith("cloud:")) return { source: "cloud", detail: name.slice("cloud:".length) };
  if (!run.dataset_id && run.session_ids.length > 0) {
    return { source: "sessions", detail: String(run.session_ids.length) };
  }
  return { source: "dataset", detail: name };
}

function runStatus(run: EvaluationRunInfo): TaskStatus {
  switch (run.status) {
    case "queued":
      return "queued";
    case "invoking":
    case "waiting":
    case "evaluating":
      return "running";
    default:
      return run.status;
  }
}

export function taskFromRun(run: EvaluationRunInfo): V2Task {
  const { source, detail } = runSource(run);
  return {
    kind: "run",
    id: run.id,
    name: run.name || `${run.agent_name} · ${detail || run.id}`,
    description: run.description ?? "",
    agentId: run.agent_id,
    agentName: run.agent_name,
    evaluators: run.evaluators,
    source,
    sourceDetail: detail,
    status: runStatus(run),
    createdAt: run.created_at ?? null,
    updatedAt: run.updated_at ?? run.created_at ?? null,
    run,
  };
}

function onlineStatus(row: OnlineEvalConfigRow): TaskStatus {
  const status = (row.status ?? "").toUpperCase();
  if (row.failure_reason || status.includes("FAIL")) return "failed";
  if (status === "CREATING" || status === "UPDATING") return "pending";
  return (row.execution_status ?? "").toUpperCase() === "ENABLED" ? "running" : "paused";
}

export function taskFromOnline(row: OnlineEvalConfigRow): V2Task {
  return {
    kind: "online",
    id: row.config_id,
    name: row.description?.trim() || row.name || row.config_id,
    description: row.description ?? "",
    agentId: row.agent_id,
    agentName: row.agent_name ?? row.matched_agent?.name ?? "—",
    evaluators: row.evaluators,
    source: "live",
    sourceDetail: row.sampling_percentage == null ? "" : `${row.sampling_percentage}%`,
    status: onlineStatus(row),
    createdAt: row.created_at,
    updatedAt: row.updated_at ?? row.created_at,
    online: row,
  };
}

export const STATUS_TONE: Record<TaskStatus, TagTone> = {
  queued: "gray",
  pending: "gray",
  running: "blue",
  completed: "green",
  failed: "red",
  stopped: "gray",
  paused: "orange",
};

export function statusLabel(t: TFunction, status: TaskStatus): string {
  return t(`v2.taskStatus.${status}`);
}

export function sourceLabel(t: TFunction, task: Pick<V2Task, "source" | "sourceDetail">): string {
  return t(`v2.taskSource.${task.source}`, { detail: task.sourceDetail });
}

/** "Agent 任务完成度 等 3 个" — first evaluator plus a count. */
export function evaluatorSummary(
  t: TFunction,
  ids: string[],
  label: (id: string) => string,
): string {
  if (ids.length === 0) return "—";
  if (ids.length === 1) return label(ids[0]);
  return t("v2.tasks.evaluatorsMore", { first: label(ids[0]), count: ids.length });
}

export function sortTasks(tasks: V2Task[]): V2Task[] {
  return [...tasks].sort((a, b) => String(b.createdAt ?? "").localeCompare(String(a.createdAt ?? "")));
}

/** Every operator task of the workspace: scored batch runs + agent-owned
 *  scores-mode online configs (experiment arms and external configs are not tasks). */
export async function loadTasks(): Promise<V2Task[]> {
  const [runs, online] = await Promise.all([
    api.listEvaluationRuns({ mode: "evaluators", limit: 200 }),
    // experiment arms and externally created configs are not operator tasks
    api.v2OnlineConfigs().catch(() => ({ configs: [], total: 0 })),
  ]);
  return sortTasks([
    ...runs.runs.map(taskFromRun),
    ...online.configs.filter((c) => c.owner === "agent" && c.mode === "scores").map(taskFromOnline),
  ]);
}

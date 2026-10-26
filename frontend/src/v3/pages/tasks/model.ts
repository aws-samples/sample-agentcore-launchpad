import type { CostSummary } from "../../../lib/api/evalCost";
import type { ResultRow } from "../../../v2/results";
import { type NewStrategy, newTaskParams } from "../../../v2/pages/tasks/urls";
import type { TaskStatus, V2Task } from "../../../v2/tasks";
import type { Signal } from "../../ui";

/** A task's status on the V3 signal scale: failed needs you, in flight is live. */
export function taskSignal(status: TaskStatus): Signal {
  switch (status) {
    case "failed":
      return "act";
    case "running":
      return "info";
    case "queued":
    case "pending":
    case "paused":
      return "wait";
    case "completed":
      return "ok";
    default:
      return "off";
  }
}

/** failed first, then what is moving, then the rest — newest first inside each */
const RANK: Record<TaskStatus, number> = { failed: 0, running: 1, queued: 1, pending: 1, paused: 2, completed: 3, stopped: 3 };
export function rankTasks<T extends { status: TaskStatus; createdAt: string | null }>(tasks: T[]): T[] {
  return [...tasks].sort((a, b) => RANK[a.status] - RANK[b.status] || String(b.createdAt ?? "").localeCompare(String(a.createdAt ?? "")));
}

export function outcomeSignal(outcome: ResultRow["outcome"]): Signal {
  return outcome === "passed" ? "ok" : outcome === "failed" ? "act" : "wait";
}

export function scoreSignal(normalized: number | null): Signal | undefined {
  if (normalized == null) return undefined;
  return normalized >= 0.7 ? "ok" : normalized >= 0.4 ? "wait" : "act";
}

export const detailUrl = (task: Pick<V2Task, "kind" | "id">) =>
  `/v3/tasks?view=detail&kind=${task.kind}&id=${encodeURIComponent(task.id)}`;
export const copyUrl = (task: Pick<V2Task, "kind" | "id">) =>
  `/v3/tasks?view=new&from=${encodeURIComponent(`${task.kind}:${task.id}`)}`;

/** The create form of a run strategy (the strategy switch), carrying only the agent. */
export const strategyUrl = (strategy: NewStrategy, agentId?: string | null) =>
  `/v3/tasks?${new URLSearchParams(newTaskParams(strategy, agentId)).toString()}`;

/* ── evaluation cost (eval-cost §1.1, §1.3) ──────────────────────────────── */

/** The run payload's own cost summary (`run.cost`, newer servers), when it carries one. */
export function runCostOf(task: Pick<V2Task, "kind" | "run">): CostSummary | null {
  if (task.kind !== "run") return null;
  const cost = task.run?.cost;
  return cost && typeof cost === "object" && "kind" in cost ? cost : null;
}

/** The cost column: the run's own summary first, `/api/eval/run-costs` for the rest. */
export function listCosts(tasks: Pick<V2Task, "kind" | "id" | "run">[], fetched: Record<string, CostSummary> | undefined) {
  const out: Record<string, CostSummary> = { ...(fetched ?? {}) };
  for (const task of tasks) {
    const own = runCostOf(task);
    if (own) out[task.id] = own;
  }
  return out;
}

/** The runs on screen whose payload carries no cost: the only ones the batch read needs. */
export const runsWithoutCost = (tasks: Pick<V2Task, "kind" | "id" | "run">[]) =>
  tasks.filter((task) => task.kind === "run" && !runCostOf(task)).map((task) => task.id);

/** A passive run (picked sessions or a look-back window) — priced and confirmed like a replay. */
export const isPassiveScope = (scope: { lookback_hours?: number; session_ids?: string[] }) =>
  Boolean(scope.lookback_hours) || Boolean(scope.session_ids?.length);

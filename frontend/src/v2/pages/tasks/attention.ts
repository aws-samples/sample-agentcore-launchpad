// The evaluation tasks that failed, newest first (ported from the V3 console). Pure: the
// task list turns each entry into a row and owns the copy and the navigation.
import { parseTimestamp } from "../../../lib/timestamps";
import type { Signal } from "../../signals";
import type { V2Task } from "../../tasks";

export interface TaskEntry {
  key: string;
  signal: Signal;
  task: V2Task;
  /** first line of the run error / online failure reason, clipped; "" when none */
  detail: string;
}

/** Rows the panel shows (`AttentionPanel` `cap`); its header counts every listed failure. */
export const TASK_ATTENTION_CAP = 5;
/** Longest hint a row shows before it is cut. */
export const HINT_MAX = 120;
/** A failed run is listed this long after it was created, unless a later run fixed it. */
export const FAILED_RUN_TTL_MS = 7 * 24 * 60 * 60 * 1000;

const clip = (text: string) => (text.length > HINT_MAX ? `${text.slice(0, HINT_MAX - 1)}…` : text);
const firstLine = (text: string | null | undefined) => (text ?? "").trim().split("\n")[0].trim();
// ledger times are naive UTC: parse, never compare strings or `new Date` them
const createdAt = (task: V2Task) => parseTimestamp(task.createdAt)?.getTime();
const createdMs = (task: V2Task) => createdAt(task) ?? 0;

/** What a run was measured on, so a later completed run can stand in for a failed one: the
 *  same mode and agent (a log source by its service), and the same dataset — or, for a run
 *  with none (window, sessions, logs), the same scope kind. */
function benchmarkOf(task: V2Task): string {
  const agent = task.agentId ?? `logs:${task.logSource?.service_name ?? task.agentName}`;
  const datasetId = task.run?.dataset_id;
  return [task.mode, agent, datasetId ? `${task.source}:${datasetId}` : task.source].join("|");
}

/**
 * Failed tasks (act), newest first; the panel caps them at `TASK_ATTENTION_CAP`. A failed
 * run expires: it is listed only within `FAILED_RUN_TTL_MS` of its creation (a run whose
 * time cannot be read is kept) and until a later run of the same agent on the same
 * benchmark completes. A failed online config is live state, not history: it stays.
 */
export function failedTasks(tasks: V2Task[], now = Date.now()): TaskEntry[] {
  const completed = new Map<string, number>();
  for (const task of tasks) {
    if (task.kind !== "run" || task.status !== "completed") continue;
    const key = benchmarkOf(task);
    completed.set(key, Math.max(completed.get(key) ?? -Infinity, createdMs(task)));
  }
  const listed = (task: V2Task) => {
    if (task.kind !== "run") return true;
    const at = createdAt(task);
    if (at !== undefined && now - at > FAILED_RUN_TTL_MS) return false;
    const fixedAt = completed.get(benchmarkOf(task));
    return fixedAt === undefined || fixedAt <= createdMs(task);
  };
  return tasks
    .filter((task) => task.status === "failed" && listed(task))
    .sort((a, b) => createdMs(b) - createdMs(a))
    .map((task) => ({
      key: `${task.kind}:${task.id}`,
      signal: "act" as const,
      task,
      detail: clip(firstLine(task.run?.error || task.online?.failure_reason)),
    }));
}

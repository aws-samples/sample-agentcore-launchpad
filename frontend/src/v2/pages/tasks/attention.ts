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

/** Rows the panel shows; its header still counts every failed task. */
export const TASK_ATTENTION_CAP = 5;
/** Longest hint a row shows before it is cut. */
export const HINT_MAX = 120;

const clip = (text: string) => (text.length > HINT_MAX ? `${text.slice(0, HINT_MAX - 1)}…` : text);
const firstLine = (text: string | null | undefined) => (text ?? "").trim().split("\n")[0].trim();
// ledger times are naive UTC: parse, never compare strings or `new Date` them
const createdMs = (task: V2Task) => parseTimestamp(task.createdAt)?.getTime() ?? 0;

/** Failed tasks (act), newest first, at most `TASK_ATTENTION_CAP`; `total` counts them all. */
export function failedTasks(tasks: V2Task[]): { entries: TaskEntry[]; total: number } {
  const failed = tasks.filter((task) => task.status === "failed");
  const entries = [...failed]
    .sort((a, b) => createdMs(b) - createdMs(a))
    .slice(0, TASK_ATTENTION_CAP)
    .map((task) => ({
      key: `${task.kind}:${task.id}`,
      signal: "act" as const,
      task,
      detail: clip(firstLine(task.run?.error || task.online?.failure_reason)),
    }));
  return { entries, total: failed.length };
}

// The Skill Lab jobs of every type that are in flight or stopped badly (ported from the
// V3 console). Pure: the page turns each entry into a row and owns the copy and the
// navigation.
import type { SkillLabJobInfo } from "../../../lib/api";
import { parseTimestamp } from "../../../lib/timestamps";
import { rankBySignal, type Signal } from "../../signals";

export interface JobEntry {
  key: string;
  signal: Signal;
  /** a running job breathes; a queued one waits still */
  live: boolean;
  job: SkillLabJobInfo;
  /** first line of the error, clipped; "" when the job has none */
  detail: string;
}

/** Rows the panel shows; its header still counts every job that needs a look. */
export const JOB_ATTENTION_CAP = 6;
/** Longest hint a row shows before it is cut. */
export const HINT_MAX = 120;

const clip = (text: string) => (text.length > HINT_MAX ? `${text.slice(0, HINT_MAX - 1)}…` : text);
// ledger times are naive UTC: parse, never compare strings or `new Date` them
const createdMs = (job: SkillLabJobInfo) => parseTimestamp(job.created_at)?.getTime() ?? 0;

/** Queued / running jobs wait (someone may cancel them); failed / interrupted ones need
 *  the user (read the error, resume). Succeeded and cancelled jobs are not queued. */
export function jobSignal(job: Pick<SkillLabJobInfo, "status">): Signal | null {
  switch (job.status) {
    case "queued":
    case "running":
      return "wait";
    case "failed":
    case "interrupted":
      return "act";
    default:
      return null;
  }
}

/** Act before wait, newest first within each, at most `JOB_ATTENTION_CAP`; `total` counts all. */
export function jobQueue(jobs: SkillLabJobInfo[]): { entries: JobEntry[]; total: number } {
  const out: JobEntry[] = [];
  for (const job of jobs) {
    const signal = jobSignal(job);
    if (!signal) continue;
    out.push({
      key: `${job.type}:${job.id}`,
      signal,
      live: job.status === "running",
      job,
      detail: clip((job.error ?? "").trim().split("\n")[0].trim()),
    });
  }
  const newest = out.sort((a, b) => createdMs(b.job) - createdMs(a.job));
  return { entries: rankBySignal(newest).slice(0, JOB_ATTENTION_CAP), total: out.length };
}

/** The URL params of a job's own page: taskgen lives inside the task-set tab. */
export function jobParams(job: Pick<SkillLabJobInfo, "type" | "id">): Record<string, string> {
  return job.type === "taskgen" ? { tab: "tasksets", view: "gen", id: job.id } : { tab: job.type, view: "detail", id: job.id };
}

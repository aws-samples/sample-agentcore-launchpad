// Data for the V3 evaluation overview — the same reads, caps and merges as V2's
// Insights page (v2/pages/Insights.tsx, v2/InsightsPanel.tsx), kept here so the V3
// page renders them its own way.
import { FAILURE_ANALYSIS_ID } from "../../../lib/agent-findings";
import { api, type V2Range } from "../../../lib/api";
import type { InsightCluster } from "../../../lib/evaluation";
import { parseTimestamp } from "../../../lib/timestamps";
import { RANGE_HOURS } from "../../../v2/format";
import { type ResultRow, rowsFromOnline, rowsFromRun } from "../../../v2/results";
import { loadTasks, type V2Task } from "../../../v2/tasks";

/** At most this many completed runs are read per refresh (one results call each). */
export const MAX_RUNS = 12;
const CONCURRENCY = 4;

async function mapLimited<T, R>(items: T[], limit: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker));
  return out;
}

export interface InsightData {
  rows: ResultRow[];
  tasks: V2Task[];
  /** completed insights runs in range — their clusters feed the insights panel */
  insightTasks: V2Task[];
  batchRuns: V2Task[];
  skippedRuns: number;
  failedReads: number;
}

export async function loadInsights(range: V2Range): Promise<InsightData> {
  const tasks = await loadTasks();
  const since = Date.now() - RANGE_HOURS[range] * 3_600_000;
  const completed = tasks.filter(
    (task) => task.kind === "run" && task.status === "completed" && task.createdAt && new Date(task.createdAt).getTime() >= since,
  );
  // insights runs cluster sessions instead of scoring them: their trees ride on the
  // run row (no results read) and go to the insights panel, not the score tables
  const runs = completed.filter((task) => task.mode === "evaluators");
  const insightTasks = completed.filter((task) => task.mode === "insights");
  const online = tasks.filter((task) => task.kind === "online");
  let failedReads = 0;
  const runRows = await mapLimited(runs.slice(0, MAX_RUNS), CONCURRENCY, async (task) => {
    try {
      return rowsFromRun(task, await api.evaluationRunResults(task.id));
    } catch {
      failedReads += 1;
      return [];
    }
  });
  const onlineRows = await mapLimited(online, CONCURRENCY, async (task) => {
    try {
      return rowsFromOnline(task, (await api.v2OnlineResults(task.id, range)).recent);
    } catch {
      failedReads += 1;
      return [];
    }
  });
  const rows = [...runRows.flat(), ...onlineRows.flat()].sort((a, b) => String(b.time ?? "").localeCompare(String(a.time ?? "")));
  const batchRuns = tasks.filter((task) => task.kind === "run" && task.mode === "evaluators"
    && task.evaluators.includes(FAILURE_ANALYSIS_ID) && (parseTimestamp(task.createdAt)?.getTime() ?? 0) >= since);
  return { rows, tasks: [...runs, ...online], insightTasks, batchRuns, skippedRuns: Math.max(0, runs.length - MAX_RUNS), failedReads };
}

export const SCORE_BANDS = {
  low: [0, 0.4],
  mid: [0.4, 0.7],
  high: [0.7, 1.0001],
} as const;
export type ScoreBand = keyof typeof SCORE_BANDS;

/* ── insight clusters ──────────────────────────────────────────────────── */

export type Section = "failures" | "userIntents" | "executionSummaries";
export const SECTIONS: Section[] = ["failures", "userIntents", "executionSummaries"];

/** One cluster name across every insights task in scope (runs name clusters independently). */
export interface MergedCluster {
  key: string;
  section: Section;
  name: string;
  description: string;
  sessions: number;
  sessionIds: string[];
  recommendation: string | null;
  members: { task: V2Task; cluster: InsightCluster }[];
}

const clusterName = (c: InsightCluster, i: number) => (c.name ?? c.category ?? `#${i + 1}`).trim();
const sessionCount = (c: InsightCluster) => c.affectedSessionCount ?? c.affectedSessions?.length ?? 0;

/** Merge the three insight trees of every task, biggest cluster first. */
export function mergeClusters(tasks: V2Task[]): Record<Section, MergedCluster[]> {
  const out: Record<Section, MergedCluster[]> = { failures: [], userIntents: [], executionSummaries: [] };
  for (const section of SECTIONS) {
    const byKey = new Map<string, MergedCluster>();
    for (const task of tasks) {
      (task.run?.insights?.[section] ?? []).forEach((cluster, i) => {
        const name = clusterName(cluster, i);
        const key = `${section}:${name.toLowerCase()}`;
        const merged =
          byKey.get(key) ??
          byKey
            .set(key, { key, section, name, description: "", sessions: 0, sessionIds: [], recommendation: null, members: [] })
            .get(key)!;
        merged.sessions += sessionCount(cluster);
        merged.description ||= cluster.description ?? "";
        merged.recommendation ||=
          cluster.subCategories?.flatMap((s) => s.rootCauses ?? []).find((r) => r.recommendation)?.recommendation ?? null;
        for (const s of cluster.affectedSessions ?? []) {
          if (s.sessionId && !merged.sessionIds.includes(s.sessionId)) merged.sessionIds.push(s.sessionId);
        }
        merged.members.push({ task, cluster });
      });
    }
    out[section] = [...byKey.values()].sort((a, b) => b.sessions - a.sessions || a.name.localeCompare(b.name));
  }
  return out;
}

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type V2Range } from "../../lib/api";
import { evaluatorLabel } from "../../lib/evaluators";
import { RANGE_HOURS, RANGES, rangeLabel } from "../format";
import { useLoad } from "../hooks";
import { EvaluatorBreakdown, ResultsTable, SummaryKpis } from "../ResultsView";
import { type ResultRow, rowsFromOnline, rowsFromRun, summarize } from "../results";
import { loadTasks, type V2Task } from "../tasks";
import { Alert, Card, FilterSelect, PageHeader, SearchInput } from "../ui";

/** At most this many completed runs are read per refresh (one results call each). */
const MAX_RUNS = 12;
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

interface InsightData {
  rows: ResultRow[];
  tasks: V2Task[];
  skippedRuns: number;
  failedReads: number;
}

async function loadInsights(range: V2Range): Promise<InsightData> {
  const tasks = await loadTasks();
  const since = Date.now() - RANGE_HOURS[range] * 3_600_000;
  const runs = tasks.filter(
    (task) => task.kind === "run" && task.status === "completed" && task.createdAt && new Date(task.createdAt).getTime() >= since,
  );
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
  return { rows, tasks: [...runs, ...online], skippedRuns: Math.max(0, runs.length - MAX_RUNS), failedReads };
}

const SCORE_BANDS = {
  low: [0, 0.4],
  mid: [0.4, 0.7],
  high: [0.7, 1.0001],
} as const;

/**
 * 分析洞察 — every judged result of the recent evaluation tasks in one place:
 * KPIs (count, normalized mean, Bad Case, pass rate), per-evaluator means and
 * a filterable result table with export and "bad case → dataset".
 */
export function V2Insights() {
  const { t } = useTranslation();
  const [range, setRange] = useState<V2Range>("7d");
  const { data, loading, error, reload } = useLoad(() => loadInsights(range), `insights:${range}`);
  const [task, setTask] = useState("");
  const [evaluator, setEvaluator] = useState("");
  const [outcome, setOutcome] = useState("");
  const [agent, setAgent] = useState("");
  const [source, setSource] = useState("");
  const [band, setBand] = useState("");
  const [q, setQ] = useState("");

  const all = useMemo(() => data?.rows ?? [], [data]);
  const options = useMemo(
    () => ({
      tasks: [...new Map(all.map((r) => [r.taskKey, r.taskName])).entries()],
      evaluators: [...new Set(all.map((r) => r.evaluatorId))],
      agents: [...new Set(all.map((r) => r.agent))].sort(),
    }),
    [all],
  );
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter((r) => {
      if (task && r.taskKey !== task) return false;
      if (evaluator && r.evaluatorId !== evaluator) return false;
      if (outcome && r.outcome !== outcome) return false;
      if (agent && r.agent !== agent) return false;
      if (source && r.source !== source) return false;
      if (band) {
        const [lo, hi] = SCORE_BANDS[band as keyof typeof SCORE_BANDS];
        if (r.normalized == null || r.normalized < lo || r.normalized >= hi) return false;
      }
      return !needle || `${r.label ?? ""} ${r.explanation ?? ""} ${r.sessionId ?? ""} ${r.taskName}`.toLowerCase().includes(needle);
    });
  }, [all, task, evaluator, outcome, agent, source, band, q]);
  const summary = useMemo(() => summarize(rows), [rows]);

  return (
    <>
      <PageHeader title={t("v2.insights.title")} desc={t("v2.insights.desc")} />
      <Card>
        <div className="v2-toolbar">
          <FilterSelect
            label={t("v2.common.timeRange")}
            value={range}
            onChange={(v) => setRange(v as V2Range)}
            options={RANGES.map((r) => ({ value: r, label: rangeLabel(t, r) }))}
          />
          <FilterSelect label={t("v2.insights.colTask")} value={task} allLabel={t("v2.common.all")} onChange={setTask} options={options.tasks.map(([value, label]) => ({ value, label }))} />
          <FilterSelect
            label={t("v2.insights.colEvaluator")}
            value={evaluator}
            allLabel={t("v2.common.all")}
            onChange={setEvaluator}
            options={options.evaluators.map((id) => ({ value: id, label: evaluatorLabel(t, id) }))}
          />
          <FilterSelect
            label={t("v2.insights.colOutcome")}
            value={outcome}
            allLabel={t("v2.common.all")}
            onChange={setOutcome}
            options={(["passed", "failed", "error"] as const).map((o) => ({ value: o, label: t(`v2.outcome.${o}`) }))}
          />
          <FilterSelect
            label={t("v2.insights.scoreBand")}
            value={band}
            allLabel={t("v2.common.all")}
            onChange={setBand}
            options={(["low", "mid", "high"] as const).map((b) => ({ value: b, label: t(`v2.insights.band.${b}`) }))}
          />
          <FilterSelect label="Agent" value={agent} allLabel={t("v2.common.all")} onChange={setAgent} options={options.agents.map((a) => ({ value: a, label: a }))} />
          <FilterSelect
            label={t("v2.tasks.colSource")}
            value={source}
            allLabel={t("v2.common.all")}
            onChange={setSource}
            options={(["window", "sessions", "dataset", "cloud", "live"] as const).map((s) => ({ value: s, label: t(`v2.taskSource.${s}Short`) }))}
          />
          <div className="end">
            <SearchInput value={q} onChange={setQ} placeholder={t("v2.insights.search")} />
          </div>
        </div>
        {data && data.skippedRuns > 0 && <Alert tone="warn">{t("v2.insights.skippedRuns", { count: data.skippedRuns, max: MAX_RUNS })}</Alert>}
        {data && data.failedReads > 0 && <Alert tone="warn">{t("v2.insights.failedReads", { count: data.failedReads })}</Alert>}
        <p className="v2-muted" style={{ fontSize: 13 }}>
          {t("v2.insights.scope", { runs: data?.tasks.filter((x) => x.kind === "run").length ?? 0, online: data?.tasks.filter((x) => x.kind === "online").length ?? 0 })}
        </p>
      </Card>
      <SummaryKpis summary={summary} />
      <EvaluatorBreakdown summary={summary} />
      <Card title={t("v2.insights.details")}>
        <ResultsTable rows={rows} loading={loading} error={error} onRetry={reload} range={range} exportName={`insights-${range}`} />
      </Card>
    </>
  );
}

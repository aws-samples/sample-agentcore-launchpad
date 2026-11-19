import "./insights.css";

import { RefreshCw, Search } from "lucide-react";
import { FindingsBrowser } from "../../components/AgentFindings";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import type { V2Range } from "../../lib/api";
import { evaluatorLabel } from "../../lib/evaluators";
import { RANGES, rangeLabel } from "../../v2/format";
import { summarize } from "../../v2/results";
import { TASK_SOURCES } from "../../v2/tasks";
import { useWorkspace } from "../../workspace/workspace-context";
import { useLoad } from "../hooks";
import { Btn, Filters, Notice, PageHead } from "../ui";
import { loadInsights, MAX_RUNS, SCORE_BANDS, type ScoreBand } from "./insights/data";
import { ClustersPanel, EvaluatorBars, FeedbackPanel, ResultsPanel, SummaryStats } from "./insights/parts";
import { Select as Dropdown } from "../select";

function Select({ label, value, onChange, options }: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
}) {
  const { t } = useTranslation();
  return (
    <label className="v3-ins-select">
      <span>{label}</span>
      <Dropdown className="v3-select" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">{t("v2.common.all")}</option>
        {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </Dropdown>
    </label>
  );
}

/**
 * 评估总览 — every judged result of the recent evaluation tasks in one place, led by
 * the Bad Cases: KPIs, per-evaluator means (weakest first), the insights runs'
 * failure / intent / summary clusters, user thumbs, and the filterable results with
 * export and "bad case → dataset". Same reads and caps as V2's page.
 */
export function V3Insights() {
  const { t } = useTranslation();
  const { current } = useWorkspace();
  const [range, setRange] = useState<V2Range>("7d");
  const [tick, setTick] = useState(0);
  const { data, loading, error } = useLoad(() => loadInsights(range), `v3-insights:${current?.id ?? ""}:${range}:${tick}`);
  const [task, setTask] = useState("");
  const [evaluator, setEvaluator] = useState("");
  const [outcome, setOutcome] = useState("");
  const [agent, setAgent] = useState("");
  const [source, setSource] = useState("");
  const [band, setBand] = useState("");
  const [q, setQ] = useState("");

  const all = useMemo(() => data?.rows ?? [], [data]);
  const insightTasks = useMemo(() => data?.insightTasks ?? [], [data]);
  const options = useMemo(
    () => ({
      tasks: [
        ...new Map([
          ...all.map((r) => [r.taskKey, r.taskName] as const),
          ...insightTasks.map((x) => [`${x.kind}:${x.id}`, x.name] as const),
        ]).entries(),
      ],
      evaluators: [...new Set(all.map((r) => r.evaluatorId))],
      agents: [...new Set([...all.map((r) => r.agent), ...insightTasks.map((x) => x.agentName)])].sort(),
    }),
    [all, insightTasks],
  );
  // evaluator / outcome / score-band filters are score-only; the rest narrow the insights too
  const insightScope = useMemo(
    () => insightTasks.filter((x) => (!task || `${x.kind}:${x.id}` === task) && (!agent || x.agentName === agent) && (!source || x.source === source)),
    [insightTasks, task, agent, source],
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
        const [lo, hi] = SCORE_BANDS[band as ScoreBand];
        if (r.normalized == null || r.normalized < lo || r.normalized >= hi) return false;
      }
      return !needle || `${r.label ?? ""} ${r.explanation ?? ""} ${r.sessionId ?? ""} ${r.taskName}`.toLowerCase().includes(needle);
    });
  }, [all, task, evaluator, outcome, agent, source, band, q]);
  const summary = useMemo(() => summarize(rows), [rows]);
  const filtered = Boolean(task || evaluator || outcome || agent || source || band || q.trim());

  const headline = !data
    ? t("v3.insights.title")
    : summary.total === 0
      ? t("v3.insights.headlineEmpty")
      : summary.failed > 0
        ? t("v3.insights.headlineBad", { count: summary.failed })
        : t("v3.insights.headlineClean", { count: summary.total });

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={`${t("v3.insights.eyebrow")} · ${rangeLabel(t, range)}`}
        title={headline}
        sub={t("v3.insights.sub")}
        end={
          <>
            <Filters value={range} onChange={setRange} options={RANGES.map((r) => ({ value: r, label: rangeLabel(t, r) }))} />
            <Btn kind="ghost" onClick={() => setTick((n) => n + 1)} title={t("v3.insights.refresh")}><RefreshCw size={14} /></Btn>
          </>
        }
      />

      {data && data.skippedRuns > 0 && <Notice s="wait">{t("v2.insights.skippedRuns", { count: data.skippedRuns, max: MAX_RUNS })}</Notice>}
      {data && data.failedReads > 0 && <Notice s="wait">{t("v2.insights.failedReads", { count: data.failedReads })}</Notice>}

      <div className="v3-ins-filters">
        <Select label={t("v2.insights.colTask")} value={task} onChange={setTask} options={options.tasks.map(([value, label]) => ({ value, label }))} />
        <Select label={t("v2.insights.colEvaluator")} value={evaluator} onChange={setEvaluator}
          options={options.evaluators.map((id) => ({ value: id, label: evaluatorLabel(t, id) }))} />
        <Select label={t("v2.insights.colOutcome")} value={outcome} onChange={setOutcome}
          options={(["passed", "failed", "error"] as const).map((o) => ({ value: o, label: t(`v2.outcome.${o}`) }))} />
        <Select label={t("v2.insights.scoreBand")} value={band} onChange={setBand}
          options={(["low", "mid", "high"] as const).map((b) => ({ value: b, label: t(`v2.insights.band.${b}`) }))} />
        <Select label="Agent" value={agent} onChange={setAgent} options={options.agents.map((a) => ({ value: a, label: a }))} />
        <Select label={t("v2.tasks.colSource")} value={source} onChange={setSource}
          options={TASK_SOURCES.map((s) => ({ value: s, label: t(`v2.taskSource.${s}Short`) }))} />
        <div className="v3-ins-search">
          <Search size={14} aria-hidden="true" />
          <input className="v3-input" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t("v2.insights.search")} aria-label={t("v2.insights.search")} />
        </div>
      </div>
      <p className="v3-ins-note" style={{ margin: "-6px 0 0" }}>
        {t("v2.insights.scope", {
          runs: data?.tasks.filter((x) => x.kind === "run").length ?? 0,
          online: data?.tasks.filter((x) => x.kind === "online").length ?? 0,
          insights: insightTasks.length,
        })}
        {filtered && <> · {t("v3.insights.filtered", { shown: rows.length, total: all.length })}</>}
      </p>

      <SummaryStats summary={summary} />
      <EvaluatorBars summary={summary} />
      <FindingsBrowser runs={data?.batchRuns ?? []} busy={loading} history={<ClustersPanel tasks={insightScope} loading={loading} needle={q} />} />
      <FeedbackPanel range={range} />
      <ResultsPanel rows={rows} loading={loading} error={error} range={range} exportName={`insights-${range}`} />
    </div>
  );
}

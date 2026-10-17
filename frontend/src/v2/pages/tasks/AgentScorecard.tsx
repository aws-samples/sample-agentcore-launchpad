import { GitCompareArrows, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { methodLabel } from "../../../components/methodChipMeta";
import { api, type EvaluationScorecard, type ScorecardAgent, type ScorecardRun } from "../../../lib/api";
import { evaluatorLabel } from "../../../lib/evaluators";
import { BudgetNoticeAlert } from "../../BudgetBanner";
import { runScore } from "../../compare";
import { fmtTime } from "../../format";
import { useLoad, usePaged } from "../../hooks";
import { Score } from "../../ResultsView";
import {
  type Benchmark,
  benchEvaluators,
  benchmarksOf,
  composite,
  coreEvaluators,
  headline,
  isActive,
  isScored,
  matrixColumns,
  rerunSource,
  revisionMarkers,
  runMean,
  seriesColor,
  seriesSlots,
  shortDate,
  shortTime,
  togglePick,
  versionBands,
  versionMatrix,
} from "../../scorecard";
import { STATUS_TONE } from "../../tasks";
import { Alert, Button, Card, type Column, FlowHeader, Kpi, LinkButton, OptionCard, Pager, Spin, Table, Tag } from "../../ui";
import { noteLabel } from "../agents/snapshots";
import { backState, returnPath } from "./back";
import { RerunDialog } from "./RerunDialog";
import {
  benchName,
  evaluatorList,
  rerunBlockText,
  runName,
  runSourceText,
  taskStatusOf,
  versionShort,
} from "./scorecardText";
import { DeltaChip, VersionTag } from "./scorecardUi";
import { TrendChart, type TrendSeries } from "./TrendChart";
import { VersionMatrix } from "./VersionMatrix";

const POLL_MS = 8000;
const RUNS_PAGE = 10;
const COMPOSITE_KEY = "__composite";

/** The agent's scorecard, re-read every 8 s while one of its runs is still active (like
 *  the task detail). A log source has no agent id: it reads the whole scorecard. */
function useAgentScorecard(agentKey: string) {
  const logSource = agentKey.startsWith("logs:");
  const [tick, setTick] = useState(0);
  const sc = useLoad<EvaluationScorecard>(
    () => api.evaluationScorecard(logSource ? {} : { agent_id: agentKey }),
    `scorecard-agent:${agentKey}:${tick}`,
  );
  const runs = useMemo(() => (sc.data ? sc.data.runs.filter((run) => run.agent_key === agentKey) : []), [sc.data, agentKey]);
  const active = runs.some(isActive);
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => window.clearInterval(timer);
  }, [active]);
  const reload = useCallback(() => setTick((n) => n + 1), []);
  const agent = sc.data?.agents.find((a) => a.key === agentKey) ?? null;
  return { ...sc, reload, runs, agent };
}

/**
 * One agent's evaluation page (`?view=agent&key=<agentKey>&bench=<benchmarkKey>`): KPIs,
 * the per-benchmark trend banded by version, version × evaluator, the run list with
 * compare picks, and 再评一次 on the current benchmark's latest completed run.
 */
export function AgentScorecard({ agentKey }: { agentKey: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const { can } = useAuth();
  const sc = useAgentScorecard(agentKey);
  const { runs, agent } = sc;
  const [picked, setPicked] = useState<string[]>([]);
  const [rerun, setRerun] = useState<ScorecardRun | null>(null);
  const [lastStarted, setLastStarted] = useState<string | null>(null);
  const mayRun = can("eval.run");

  const benches = useMemo(() => benchmarksOf(runs), [runs]);
  const bench = benches.find((b) => b.key === params.get("bench")) ?? benches[0] ?? null;
  const order = useMemo(() => runs.map((run) => run.id), [runs]);
  // one colour per evaluator for the whole page, so a benchmark switch never repaints one
  const slots = useMemo(() => seriesSlots(benchEvaluators(runs)), [runs]);

  const selectBench = (key: string) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set("bench", key);
        return next;
      },
      // a view toggle: keep where Back goes
      { replace: true, state: location.state },
    );
  const pick = (run: ScorecardRun) => setPicked((prev) => togglePick(prev, run.id, order));
  const openCompare = (a: string, b: string) => setParams({ view: "compare", a, b });
  // to the page outside 评估任务 that opened this one (the architect's step 2), else the list
  const back = () => {
    const outside = returnPath(location.state);
    if (outside) navigate(outside);
    else setParams({});
  };
  const openRun = (id: string) => setParams({ view: "detail", kind: "run", id }, { state: backState(location.search) });

  if (sc.loading && !sc.data) return <Spin />;
  if (sc.error && !sc.data) {
    return (
      <>
        <FlowHeader title={t("v2.scorecard.agent.title")} onBack={back} />
        <Alert tone="error" action={<LinkButton onClick={sc.reload}>{t("v2.common.retry")}</LinkButton>}>
          {sc.error}
        </Alert>
      </>
    );
  }
  if (!agent) {
    return (
      <>
        <FlowHeader title={t("v2.scorecard.agent.title")} onBack={back} />
        <Alert tone="warn">{t("v2.scorecard.agent.notFound")}</Alert>
      </>
    );
  }

  const target = rerunSource(bench, agent);
  const blocked = rerunBlockText(t, target.block, mayRun);
  const pickedRuns = picked.map((id) => runs.find((run) => run.id === id)).filter((run): run is ScorecardRun => !!run);
  // the budget notice of a re-run started here, or from the list (which opens this page)
  const noticeId = lastStarted ?? [...runs].reverse().find(isActive)?.id ?? null;

  return (
    <div className="v2-scorecard" data-testid="v2-scorecard-agent">
      <FlowHeader
        title={
          <span className="v2-row">
            <span className={agent.log_source ? "mono" : undefined}>{agent.name}</span>
            {agent.log_source ? (
              <Tag tone="outline">{t("v2.tasks.cw.short")}</Tag>
            ) : (
              agent.method && <Tag tone="outline">{methodLabel(agent.method)}</Tag>
            )}
            {agent.current_seq != null && <Tag tone="blue">{t("v2.scorecard.current", { seq: agent.current_seq })}</Tag>}
            {agent.deleted && <Tag tone="gray">{t("v2.scorecard.deleted")}</Tag>}
          </span>
        }
        onBack={back}
        end={
          <>
            {agent.agent_id && !agent.deleted && (
              <Button
                onClick={() => navigate(`/v2/agents?view=detail&id=${encodeURIComponent(agent.agent_id ?? "")}`)}
                testId="v2-scorecard-agent-detail"
              >
                {t("v2.scorecard.agent.detail")}
              </Button>
            )}
            <Button
              disabled={pickedRuns.length !== 2}
              title={pickedRuns.length === 2 ? undefined : t("v2.scorecard.agent.compareTwoHint")}
              onClick={() => pickedRuns.length === 2 && openCompare(pickedRuns[0].id, pickedRuns[1].id)}
              testId="v2-scorecard-compare-picked"
            >
              <GitCompareArrows size={14} aria-hidden="true" />
              {t("v2.scorecard.agent.compareTwo")}
            </Button>
            <Button
              kind="primary"
              disabled={!!blocked || !target.run}
              title={blocked}
              onClick={() => target.run && setRerun(target.run)}
              testId="v2-scorecard-rerun-open"
            >
              <RotateCcw size={14} aria-hidden="true" />
              {t("v2.scorecard.rerun.action")}
            </Button>
          </>
        }
      />
      <BudgetNoticeAlert id={noticeId} />
      {agent.deleted && <Alert tone="warn">{t("v2.scorecard.agent.deletedNote")}</Alert>}
      {sc.data?.truncated && <Alert tone="info">{t("v2.scorecard.truncated")}</Alert>}
      {!bench ? (
        <Card>
          <div className="v2-sc-empty">
            <p>{t("v2.scorecard.agent.noRuns")}</p>
            {agent.agent_id && !agent.deleted && (
              <Button
                kind="primary"
                disabled={!mayRun}
                title={mayRun ? undefined : t("v2.tasks.noPermission")}
                onClick={() => setParams({ view: "new", agent: agent.agent_id ?? "" }, { state: backState(location.search) })}
              >
                {t("v2.tasks.new")}
              </Button>
            )}
          </div>
        </Card>
      ) : (
        <>
          <KpiRow agent={agent} bench={bench} benches={benches} />
          <TrendCard
            agent={agent}
            bench={bench}
            benches={benches}
            slots={slots}
            picked={picked}
            pickedRuns={pickedRuns}
            onBench={selectBench}
            onPick={pick}
            onClear={() => setPicked([])}
            onCompare={openCompare}
          />
          {bench.kind !== "live" && <MatrixCard agent={agent} bench={bench} slots={slots} onCompare={openCompare} />}
          <RunsCard runs={runs} bench={bench} picked={picked} onPick={pick} onOpen={openRun} />
        </>
      )}
      {rerun && (
        <RerunDialog
          key={rerun.id}
          agent={agent}
          source={rerun}
          onClose={() => setRerun(null)}
          onStarted={(run) => {
            setRerun(null);
            setLastStarted(run.id);
            sc.reload();
          }}
        />
      )}
    </div>
  );
}

function KpiRow({ agent, bench, benches }: { agent: ScorecardAgent; bench: Benchmark; benches: Benchmark[] }) {
  const { t } = useTranslation();
  const agentScored = benches.reduce((sum, b) => sum + b.scored.length, 0);
  const head = headline(bench, { agentScored, anyComparable: benches.some((b) => b.scored.length > 1) });
  const current = agent.versions.find((v) => v.seq === agent.current_seq) ?? null;
  const note = noteLabel(current?.note);
  const newest = bench.runs[bench.runs.length - 1] ?? null;
  const unfinished = bench.runs.filter((run) => run.status !== "completed").length;
  const versions = new Set(bench.scored.map((run) => run.snapshot?.seq).filter((seq) => seq != null)).size;
  const prevSeq = head.byVersion ? head.previous?.snapshot?.seq : null;
  return (
    <div className="v2-kpis v2-sc-kpis" data-testid="v2-scorecard-kpis">
      <Kpi
        label={t("v2.scorecard.kpi.latest", { bench: benchName(t, bench) })}
        value={
          <span className="v2-sc-kpi-v">
            <Score value={head.score} />
            {head.delta != null && <DeltaChip value={head.delta} testId="v2-scorecard-kpi-delta" />}
          </span>
        }
        sub={
          head.previous && !head.reason
            ? prevSeq != null
              ? t("v2.scorecard.kpi.vs", { seq: prevSeq, date: shortDate(head.previous.created_at) })
              : t("v2.scorecard.kpi.vsRun", { date: shortDate(head.previous.created_at) })
            : t(`v2.scorecard.reason.${head.reason ?? "noCompleted"}`)
        }
        testId="v2-scorecard-kpi-latest"
      />
      <Kpi
        label={t("v2.scorecard.kpi.current")}
        value={
          <span className="v2-sc-kpi-v">
            {agent.current_seq != null ? `#${agent.current_seq}` : "—"}
            {note && <small>{"text" in note ? note.text : t(note.key, note.params)}</small>}
          </span>
        }
        sub={
          agent.log_source
            ? t("v2.scorecard.kpi.noVersionLog")
            : current
              ? current.created_by_name
                ? t("v2.scorecard.kpi.published", { date: fmtTime(current.created_at), name: current.created_by_name })
                : t("v2.scorecard.kpi.publishedNoName", { date: fmtTime(current.created_at) })
              : t("v2.scorecard.kpi.noDeployed")
        }
        testId="v2-scorecard-kpi-current"
      />
      <Kpi
        label={t("v2.scorecard.kpi.benchRuns")}
        value={
          <span className="v2-sc-kpi-v">
            {bench.runs.length}
            <small>{t("v2.scorecard.kpi.runsUnit", { count: versions })}</small>
          </span>
        }
        sub={unfinished ? t("v2.scorecard.kpi.unfinished", { count: unfinished }) : t("v2.scorecard.kpi.allDone")}
        testId="v2-scorecard-kpi-runs"
      />
      <Kpi
        label={t("v2.scorecard.kpi.lastEval")}
        value={
          <span className="v2-sc-kpi-v">
            {newest ? shortDate(newest.created_at) : "—"}
            {newest && <small>{shortTime(newest.created_at)}</small>}
          </span>
        }
        sub={newest ? `${runName(newest)} · ${newest.created_by_name || "—"}` : undefined}
        testId="v2-scorecard-kpi-last"
      />
    </div>
  );
}

function TrendCard({
  agent,
  bench,
  benches,
  slots,
  picked,
  pickedRuns,
  onBench,
  onPick,
  onClear,
  onCompare,
}: {
  agent: ScorecardAgent;
  bench: Benchmark;
  benches: Benchmark[];
  slots: Map<string, number | null>;
  picked: string[];
  pickedRuns: ScorecardRun[];
  onBench: (key: string) => void;
  onPick: (run: ScorecardRun) => void;
  onClear: () => void;
  onCompare: (a: string, b: string) => void;
}) {
  const { t } = useTranslation();
  // failed / stopped runs are left out; queued and running ones are pending markers
  const chartRuns = useMemo(() => bench.runs.filter((run) => isScored(run) || isActive(run)), [bench]);
  const core = useMemo(() => coreEvaluators(bench.runs), [bench]);
  const series = useMemo<TrendSeries[]>(() => {
    const evaluators = benchEvaluators(chartRuns).sort();
    return [
      ...(core.length
        ? [
            {
              key: COMPOSITE_KEY,
              label: t("v2.compare.composite"),
              color: "var(--v2-series-composite)",
              thick: true,
              values: chartRuns.map((run) => composite(run, core)),
            },
          ]
        : []),
      ...evaluators.map((id) => ({
        key: id,
        label: evaluatorLabel(t, id),
        color: seriesColor(slots.get(id)),
        values: chartRuns.map((run) => (isScored(run) ? runScore(run, id) : null)),
      })),
    ];
  }, [chartRuns, core, slots, t]);
  const bands = useMemo(() => versionBands(chartRuns, agent.versions), [chartRuns, agent.versions]);
  const markers = useMemo(() => revisionMarkers(chartRuns), [chartRuns]);
  const name = benchName(t, bench);
  const describe = (run: ScorecardRun) =>
    [
      `${shortDate(run.created_at)} ${shortTime(run.created_at)}`,
      versionShort(t, run.snapshot) || t("v2.scorecard.noVersion"),
      runSourceText(t, run),
      run.sessions ? t("v2.compare.sessions", { count: run.sessions }) : null,
    ]
      .filter(Boolean)
      .join(" · ");
  const pickLabel = (run: ScorecardRun) =>
    [runName(run), versionShort(t, run.snapshot), shortDate(run.created_at)].filter(Boolean).join(" · ");

  return (
    <Card title={t("v2.scorecard.trend.title")} sub={t("v2.scorecard.trend.sub")} testId="v2-scorecard-trend">
      <div className="v2-options v2-sc-benches" data-testid="v2-scorecard-benches">
        {benches.map((b) => {
          const size = b.runs[b.runs.length - 1]?.benchmark.size;
          const desc = [
            t(`v2.scorecard.benchKind.${b.kind}`),
            t("v2.scorecard.benchRuns", { count: b.runs.length }),
            size != null && b.kind !== "live" ? t("v2.scorecard.benchSize", { count: size }) : null,
          ]
            .filter(Boolean)
            .join(" · ");
          return (
            <OptionCard
              key={b.key}
              title={benchName(t, b)}
              desc={desc}
              on={b.key === bench.key}
              onClick={() => onBench(b.key)}
              badge={b.kind === "live" ? <Tag tone="gray">{t("v2.scorecard.liveTag")}</Tag> : undefined}
              testId={`v2-scorecard-bench-${b.key}`}
            />
          );
        })}
      </div>
      {bench.kind === "live" ? (
        <Alert tone="warn">{t("v2.scorecard.trend.live")}</Alert>
      ) : bench.scored.length < 2 ? (
        <Alert tone="warn">{t("v2.scorecard.trend.few", { count: bench.scored.length })}</Alert>
      ) : null}
      {chartRuns.length === 0 ? (
        <p className="v2-sc-caption">{t("v2.scorecard.trend.empty")}</p>
      ) : (
        <TrendChart
          key={bench.key}
          runs={chartRuns}
          series={series}
          bands={bands}
          markers={markers}
          picked={picked}
          onPick={onPick}
          title={name}
          describe={describe}
        />
      )}
      <p className="v2-sc-caption">
        {core.length ? t("v2.scorecard.trend.caption", { list: evaluatorList(t, core) }) : t("v2.scorecard.trend.captionNoCore")}
      </p>
      <div className="v2-pickbar" data-testid="v2-scorecard-pickbar">
        {pickedRuns.length === 0 ? (
          <span>{t("v2.scorecard.trend.pickHint")}</span>
        ) : (
          <>
            <span className="v2-compare-ab sm">A</span>
            <span className="v2-pickbar-run">{pickLabel(pickedRuns[0])}</span>
            {pickedRuns[1] ? (
              <>
                <span className="v2-compare-ab sm">B</span>
                <span className="v2-pickbar-run">{pickLabel(pickedRuns[1])}</span>
              </>
            ) : (
              <span className="v2-muted">{t("v2.scorecard.trend.pickB")}</span>
            )}
            <span className="end">
              <Button size="sm" onClick={onClear} testId="v2-scorecard-pick-clear">
                {t("v2.scorecard.trend.clear")}
              </Button>
              <Button
                size="sm"
                kind="primary"
                disabled={pickedRuns.length !== 2}
                onClick={() => pickedRuns.length === 2 && onCompare(pickedRuns[0].id, pickedRuns[1].id)}
                testId="v2-scorecard-pick-compare"
              >
                {t("v2.scorecard.trend.compare")}
              </Button>
            </span>
          </>
        )}
      </div>
    </Card>
  );
}

function MatrixCard({
  agent,
  bench,
  slots,
  onCompare,
}: {
  agent: ScorecardAgent;
  bench: Benchmark;
  slots: Map<string, number | null>;
  onCompare: (a: string, b: string) => void;
}) {
  const { t } = useTranslation();
  const core = coreEvaluators(bench.runs);
  const evaluators = benchEvaluators(bench.scored).sort();
  const columns = matrixColumns(evaluators, core);
  const rows = versionMatrix(bench.scored, core, columns);
  if (!rows.length) return null;
  return (
    <Card title={t("v2.scorecard.matrix.title")} sub={t("v2.scorecard.matrix.sub")} testId="v2-scorecard-matrix-card">
      <VersionMatrix
        rows={rows}
        columns={columns}
        hiddenCount={evaluators.length - columns.length}
        slots={slots}
        versions={agent.versions}
        currentSeq={agent.current_seq}
        onCompare={(older, newer) => onCompare(older.id, newer.id)}
      />
    </Card>
  );
}

function RunsCard({
  runs,
  bench,
  picked,
  onPick,
  onOpen,
}: {
  runs: ScorecardRun[];
  bench: Benchmark;
  picked: string[];
  onPick: (run: ScorecardRun) => void;
  onOpen: (id: string) => void;
}) {
  const { t } = useTranslation();
  const [onlyBench, setOnlyBench] = useState(true);
  const list = useMemo(() => [...(onlyBench ? bench.runs : runs)].reverse(), [onlyBench, bench, runs]);
  const paged = usePaged(list, RUNS_PAGE);

  const columns: Column<ScorecardRun>[] = [
    {
      key: "pick",
      title: t("v2.scorecard.col.pick"),
      className: "nowrap",
      render: (run) => {
        const at = picked.indexOf(run.id);
        const ok = isScored(run);
        return (
          <label className={ok ? "v2-check v2-sc-pick" : "v2-check v2-sc-pick disabled"} title={ok ? undefined : t("v2.scorecard.runs.pickDisabled")}>
            <input
              type="checkbox"
              checked={at >= 0}
              disabled={!ok}
              onChange={() => onPick(run)}
              aria-label={t("v2.scorecard.runs.pickLabel", { name: runName(run) })}
              data-testid={`v2-scorecard-pick-${run.id}`}
            />
            {at >= 0 && <span className="v2-compare-ab sm">{at === 0 ? "A" : "B"}</span>}
          </label>
        );
      },
    },
    {
      key: "name",
      title: t("v2.tasks.colNameId"),
      // an unnamed run shows its id once
      render: (run) =>
        run.name ? (
          <>
            <span className="ellipsis" style={{ maxWidth: 220 }} title={run.name}>
              {run.name}
            </span>
            <span className="sub mono">ID: {run.id}</span>
          </>
        ) : (
          <span className="mono">{run.id}</span>
        ),
    },
    {
      key: "version",
      title: t("v2.scorecard.col.version"),
      className: "nowrap",
      render: (run) => <VersionTag snapshot={run.snapshot} />,
    },
    {
      key: "source",
      title: t("v2.scorecard.col.benchSource"),
      render: (run) => (
        <>
          <span className="ellipsis" style={{ maxWidth: 220 }} title={runSourceText(t, run)}>
            {runSourceText(t, run)}
          </span>
          <span className="sub ellipsis" style={{ maxWidth: 220 }} title={evaluatorList(t, run.evaluators)}>
            {[run.sessions ? t("v2.compare.sessions", { count: run.sessions }) : null, evaluatorList(t, run.evaluators)]
              .filter(Boolean)
              .join(" · ")}
          </span>
        </>
      ),
    },
    {
      key: "avg",
      title: t("v2.scorecard.col.avg"),
      className: "nowrap",
      render: (run) => <Score value={runMean(run)} />,
    },
    {
      key: "status",
      title: t("v2.tasks.colStatus"),
      render: (run) => {
        const status = taskStatusOf(run.status);
        const error = (run.error ?? "").trim().split("\n")[0];
        return (
          <>
            <Tag tone={STATUS_TONE[status]}>{t(`v2.taskStatus.${status}`)}</Tag>
            {run.status === "failed" && error && (
              <span className="sub clip" title={error}>
                {error}
              </span>
            )}
          </>
        );
      },
    },
    {
      key: "time",
      title: t("v2.tasks.colCreated"),
      className: "nowrap",
      render: (run) => (
        <>
          {fmtTime(run.created_at)}
          <span className="sub" title={t("v2.common.creator")}>
            {run.created_by_name || "—"}
          </span>
        </>
      ),
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right sticky",
      render: (run) => (
        <div className="v2-actions">
          <LinkButton onClick={() => onOpen(run.id)} testId={`v2-scorecard-run-open-${run.id}`}>
            {t("v2.common.detail")}
          </LinkButton>
        </div>
      ),
    },
  ];

  return (
    <Card
      title={t("v2.scorecard.runs.title")}
      sub={t("v2.scorecard.runs.sub")}
      end={
        <label className="v2-check">
          <input
            type="checkbox"
            checked={onlyBench}
            onChange={(e) => setOnlyBench(e.target.checked)}
            data-testid="v2-scorecard-only-bench"
          />
          {t("v2.scorecard.runs.onlyBench")}
        </label>
      }
      testId="v2-scorecard-runs-card"
    >
      <Table columns={columns} rows={paged.slice} rowKey={(run) => run.id} empty={t("v2.scorecard.runs.empty")} testId="v2-scorecard-runs" />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
    </Card>
  );
}

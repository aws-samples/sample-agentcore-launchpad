import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation } from "react-router-dom";

import { api, type EvaluationScorecard, type ScorecardAgent, type ScorecardRun } from "../../../lib/api";
import {
  coverageDiffers,
  coverageInfo,
  type EvaluationRunInfo,
  evaluationRunPresentation,
  RUN_TERMINAL_STATUSES,
  runCoverage,
} from "../../../lib/evaluation";
import { evaluatorLabel } from "../../../lib/evaluators";
import { fmtScore, fmtTime } from "../../format";
import { useLoad } from "../../hooks";
import { Score } from "../../ResultsView";
import { RunCoverageNote, RunCoverageTag } from "../../RunCoverage";
import {
  type Benchmark,
  benchEvaluators,
  benchmarksOf,
  composite,
  coreEvaluators,
  evaluatorChanges,
  headline,
  matrixColumns,
  type MatrixRow,
  type RunStep,
  runSteps,
  shortDate,
  versionMatrix,
} from "../../scorecard";
import { Alert, type Column, LinkButton, Spin, Table, Tag } from "../../ui";
import { noteLabel } from "../agents/snapshots";
import { returnState } from "../tasks/back";
import { DeltaChip, Sparkline, VersionTag } from "../tasks/scorecardUi";
import { versionShort } from "../tasks/scorecardText";
import { MatrixScore } from "../tasks/VersionMatrix";
import { CHIP_TAG, shortId } from "./common";

/**
 * Step 2's run history: this agent's runs on the Dataset the evaluation assets created,
 * read as one scorecard benchmark — the latest composite against the previous version,
 * a row per run with its version and its change, and the evaluators per version. The
 * numbers come from the same rules as 评估任务 › 按 Agent (`v2/scorecard.ts`); the
 * ledger run list the panel already polls adds the queue position and the partial-run
 * coverage, and re-reads the scorecard whenever one of its runs changes.
 */

const SHOWN = 5;
const VERSION_COLUMNS = 4;

export function RunHistory({
  agentId,
  datasetId,
  runs,
  reloadKey,
  canRun,
  removing,
  onRemove,
  reading,
  onReadDetail,
}: {
  agentId: string;
  datasetId: string;
  /** the ledger runs of this agent on this Dataset, newest first */
  runs: EvaluationRunInfo[];
  /** changes when the agent re-publishes (a new version to label) */
  reloadKey: string;
  canRun: boolean;
  removing: string | null;
  onRemove: (run: EvaluationRunInfo) => void;
  reading: string | null;
  onReadDetail: (run: EvaluationRunInfo) => void;
}) {
  const { t } = useTranslation();
  const location = useLocation();
  const signature = runs.map((r) => `${r.id}:${r.status}:${r.scores.length}`).join(",");
  const sc = useLoad<EvaluationScorecard>(
    () => api.evaluationScorecard({ agent_id: agentId }),
    `assistant-runs:${agentId}:${reloadKey}:${signature}`,
  );
  const agentRuns = useMemo(() => (sc.data ? sc.data.runs.filter((r) => r.agent_key === agentId) : []), [sc.data, agentId]);
  const benches = useMemo(() => benchmarksOf(agentRuns), [agentRuns]);
  const bench = benches.find((b) => b.key === `ds:${datasetId}`) ?? null;
  const agent = sc.data?.agents.find((a) => a.key === agentId) ?? null;
  const live = runs.some((r) => !RUN_TERMINAL_STATUSES.has(r.status));

  // Back on the task pages opened from here returns to this conversation
  const back = returnState(`${location.pathname}${location.search}`);
  const scorecardLink = `/v2/eval/tasks?${new URLSearchParams({ view: "agent", key: agentId, bench: `ds:${datasetId}` })}`;

  if (!sc.data) {
    if (sc.error) {
      return (
        <div className="v2-assistant-runs" style={{ marginTop: 10 }}>
          <Alert tone="error" action={<LinkButton onClick={sc.reload}>{t("v2.common.retry")}</LinkButton>}>
            {t("assistantNext.run.scorecardError", { message: sc.error })}
          </Alert>
        </div>
      );
    }
    return runs.length > 0 ? <Spin /> : null;
  }
  if (!bench) return null;

  const core = coreEvaluators(bench.runs);
  const steps = runSteps(bench, core);
  const agentScored = benches.reduce((sum, b) => sum + b.scored.length, 0);
  const head = headline(bench, { agentScored, anyComparable: benches.some((b) => b.scored.length > 1) });
  const spark = bench.scored.map((run) => composite(run, core)).filter((v): v is number => v != null);
  const rows = [...bench.runs].reverse().slice(0, SHOWN);
  const info = new Map(runs.map((r) => [r.id, r]));
  const coverage = new Map(rows.map((run) => {
    const row = info.get(run.id);
    return [run.id, row ? runCoverage(row) : null];
  }));
  const others = agentRuns.length - bench.runs.length;
  const prevSeq = head.byVersion ? head.previous?.snapshot?.seq : null;

  return (
    <div className="v2-assistant-runs" data-testid="v2-assistant-next-runs" data-live={live ? "true" : "false"}>
      <div className="v2-assistant-runs-head">
        <span className="v2-muted">{t("assistantNext.run.historyHead", { n: bench.runs.length })}</span>
        <Link to={scorecardLink} state={back} data-testid="v2-assistant-next-scorecard">
          {t("assistantNext.run.openScorecard")} ›
        </Link>
      </div>
      {head.latest && (
        <div className="v2-assistant-runs-kpi" data-testid="v2-assistant-next-headline">
          <span className="v2-muted">{t("assistantNext.run.latest")}</span>
          <Score value={head.score} />
          {head.delta != null && <DeltaChip value={head.delta} />}
          <span className="v2-muted">
            {head.previous && !head.reason
              ? prevSeq != null
                ? t("v2.scorecard.kpi.vs", { seq: prevSeq, date: shortDate(head.previous.created_at) })
                : t("v2.scorecard.kpi.vsRun", { date: shortDate(head.previous.created_at) })
              : t(`v2.scorecard.reason.${head.reason ?? "noCompleted"}`)}
          </span>
          <Sparkline
            values={spark}
            label={t("v2.scorecard.sparkLabel", { values: spark.map(fmtScore).join(t("v2.scorecard.listSep")) })}
          />
        </div>
      )}
      <RunTable
        rows={rows}
        steps={steps}
        agent={agent}
        info={info}
        coverage={coverage}
        back={back}
        canRun={canRun}
        removing={removing}
        onRemove={onRemove}
      />
      <div className="v2-sc-caption">
        <div>{core.length ? t("assistantNext.run.caption", { n: core.length }) : t("assistantNext.run.captionNoCore")}</div>
        {bench.runs.length > SHOWN && (
          <div>
            {t("assistantNext.run.more", { shown: SHOWN, n: bench.runs.length })}{" "}
            <Link to={scorecardLink} state={back}>{t("assistantNext.run.moreLink")} ›</Link>
          </div>
        )}
        {others > 0 && (
          <div>
            {t("assistantNext.run.otherBenches", { count: others })}{" "}
            <Link to={`/v2/eval/tasks?${new URLSearchParams({ view: "agent", key: agentId })}`} state={back}>
              {t("assistantNext.run.openScorecard")} ›
            </Link>
          </div>
        )}
      </div>
      {coverageDiffers([...coverage.values()]) && (
        <div className="v2-muted" style={{ fontSize: 12.5 }} data-testid="v2-assistant-next-not-comparable">
          {t("v2.runCoverage.notComparable")}
        </div>
      )}
      {rows.map((run) => {
        const row = info.get(run.id);
        const cov = coverage.get(run.id) ?? null;
        const error = row?.error ?? run.error;
        if (!cov && !error) return null;
        const presentation = evaluationRunPresentation(run);
        return (
          <div key={run.id} className="v2-assistant-sub-row" data-run-status={run.status}>
            <span className="mono">{t("assistantNext.run.runLine", { id: shortId(run.id) })}</span>
            {cov && row ? (
              <div className={coverageInfo(cov) ? "err info" : "err warn"}>
                <RunCoverageNote
                  coverage={cov}
                  rawError={row.error}
                  showRawError={false}
                  onReadDetail={canRun ? () => onReadDetail(row) : undefined}
                  reading={reading === run.id}
                />
              </div>
            ) : (
              <div className={presentation.status === "completed_with_errors" ? "err warn" : "err"}>{error}</div>
            )}
          </div>
        );
      })}
      <Breakdown bench={bench} core={core} agent={agent} />
    </div>
  );
}

function RunTable({
  rows,
  steps,
  agent,
  info,
  coverage,
  back,
  canRun,
  removing,
  onRemove,
}: {
  rows: ScorecardRun[];
  steps: Map<string, RunStep>;
  agent: ScorecardAgent | null;
  info: Map<string, EvaluationRunInfo>;
  coverage: Map<string, ReturnType<typeof runCoverage>>;
  back: ReturnType<typeof returnState>;
  canRun: boolean;
  removing: string | null;
  onRemove: (run: EvaluationRunInfo) => void;
}) {
  const { t } = useTranslation();
  const bySeq = new Map((agent?.versions ?? []).map((v) => [v.seq, v]));
  // a version's note and 当前 go on its newest row only, not on every re-run of it
  const leads = new Set(rows.filter((run, i) => i === 0 || rows[i - 1].snapshot?.seq !== run.snapshot?.seq).map((run) => run.id));
  const columns: Column<ScorecardRun>[] = [
    {
      key: "version",
      title: t("v2.scorecard.col.version"),
      render: (run) => {
        const seq = run.snapshot?.seq;
        const lead = leads.has(run.id);
        const note = lead ? noteLabel(seq != null ? bySeq.get(seq)?.note : null) : null;
        return (
          <div className="v2-sc-ver">
            <b>
              <VersionTag snapshot={run.snapshot} />
              {lead && seq != null && seq === agent?.current_seq && <Tag tone="blue">{t("v2.scorecard.currentShort")}</Tag>}
            </b>
            {note && <span className="sub">{"text" in note ? note.text : t(note.key, note.params)}</span>}
          </div>
        );
      },
    },
    {
      key: "run",
      title: t("assistantNext.run.colRun"),
      className: "nowrap",
      render: (run) => <span className="mono" title={run.id}>{shortId(run.id)}</span>,
    },
    {
      key: "score",
      title: t("assistantNext.run.colScore"),
      className: "nowrap",
      render: (run) => {
        const step = steps.get(run.id);
        const cov = coverage.get(run.id);
        return (
          <>
            <span className="v2-assistant-run-score" data-testid={`v2-assistant-next-score-${run.id}`}>
              <Score value={step?.score ?? null} />
              <StepChange run={run} step={step} />
            </span>
            {cov && <span className="sub">{t("assistantNext.run.coverageSub", { scored: cov.scored, total: cov.total })}</span>}
          </>
        );
      },
    },
    {
      key: "status",
      title: t("v2.tasks.colStatus"),
      className: "nowrap",
      render: (run) => {
        const cov = coverage.get(run.id);
        const queue = info.get(run.id)?.queue_position;
        const presentation = evaluationRunPresentation(run);
        return (
          <>
            {cov ? (
              <RunCoverageTag coverage={cov} />
            ) : (
              <Tag tone={CHIP_TAG[presentation.tone] ?? "gray"}>{t(`expPage.readiness.runStatus.${presentation.status}`)}</Tag>
            )}
            {queue != null && !RUN_TERMINAL_STATUSES.has(run.status) && (
              <span className="sub">{t("assistantNext.run.queued", { n: queue })}</span>
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
          <span className="sub" title={t("v2.common.creator")}>{run.created_by_name || "—"}</span>
        </>
      ),
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (run) => {
        const step = steps.get(run.id);
        const row = info.get(run.id);
        const previous = step?.previous ?? null;
        return (
          <div className="v2-actions">
            <Link className="v2-link" to={`/v2/eval/tasks?view=detail&id=${encodeURIComponent(run.id)}`} state={back}>
              {t("v2.common.detail")}
            </Link>
            {previous && (
              <Link
                className="v2-link"
                to={`/v2/eval/tasks?${new URLSearchParams({ view: "compare", a: previous.id, b: run.id })}`}
                title={t("assistantNext.run.compareHint", {
                  id: shortId(previous.id),
                  version: versionShort(t, previous.snapshot) || t("v2.scorecard.noVersion"),
                })}
                data-testid={`v2-assistant-next-compare-${run.id}`}
              >
                {step?.sameVersion ? t("assistantNext.run.compareRun") : t("assistantNext.run.compareVersion")}
              </Link>
            )}
            {canRun && row && (run.status === "failed" || run.status === "stopped") && (
              <LinkButton danger disabled={removing === run.id} title={t("assistantNext.run.remove")} onClick={() => onRemove(row)}>
                {t("v2.common.delete")}
              </LinkButton>
            )}
          </div>
        );
      },
    },
  ];
  return <Table columns={columns} rows={rows} rowKey={(run) => run.id} density="dense" testId="v2-assistant-next-run-table" />;
}

/** The change chip of a run: against the previous version (red 退步 when it regressed by
 *  the scorecard rule), or a muted same-version difference (judge noise). */
function StepChange({ run, step }: { run: ScorecardRun; step: RunStep | undefined }) {
  const { t } = useTranslation();
  if (!step?.previous || step.delta == null) return null;
  if (step.sameVersion) {
    return (
      <span className="v2-assistant-rerun" title={t("assistantNext.run.sameVersionHint")}>
        <Tag tone="gray">{t("assistantNext.run.sameVersion")}</Tag>
        <span className="v2-muted">{`${step.delta >= 0 ? "+" : "−"}${Math.abs(step.delta).toFixed(2)}`}</span>
      </span>
    );
  }
  const worst = step.regression ? evaluatorChanges(step.previous, run)[0] : null;
  return (
    <>
      <DeltaChip value={step.delta} />
      {step.regression && (
        <Tag
          tone="red"
          title={worst ? t("v2.scorecard.attention.change", { name: evaluatorLabel(t, worst.evaluatorId), a: fmtScore(worst.a), b: fmtScore(worst.b) }) : undefined}
        >
          {t("assistantNext.run.regression")}
        </Tag>
      )}
    </>
  );
}

/** Evaluators down, agent versions across (the newest four, oldest left): the means of the
 *  version × evaluator table transposed, with the change only on the newest column. */
function Breakdown({ bench, core, agent }: { bench: Benchmark; core: string[]; agent: ScorecardAgent | null }) {
  const { t } = useTranslation();
  const evaluators = benchEvaluators(bench.scored);
  const ids = matrixColumns(evaluators, core, evaluators.length);
  const versions = versionMatrix(bench.scored, core, ids);
  if (!versions.length || !ids.length) return null;
  const shown = versions.slice(0, VERSION_COLUMNS).reverse();
  const last = shown.length - 1;
  type Line = { key: string; label: string; composite: boolean; cell: (v: MatrixRow) => MatrixRow["composite"] };
  const lines: Line[] = [
    ...(core.length ? [{ key: "__composite", label: t("v2.scorecard.matrix.composite"), composite: true, cell: (v: MatrixRow) => v.composite }] : []),
    ...ids.map((id, i) => ({ key: id, label: evaluatorLabel(t, id), composite: false, cell: (v: MatrixRow) => v.cells[i] })),
  ];
  const columns: Column<Line>[] = [
    {
      key: "evaluator",
      title: t("assistantNext.run.colEvaluator"),
      render: (line) =>
        line.composite ? <b>{line.label}</b> : <span className="ellipsis" style={{ maxWidth: 240 }} title={line.key}>{line.label}</span>,
    },
    ...shown.map(
      (v, i): Column<Line> => ({
        key: `v${v.seq}`,
        title: (
          <span className="v2-row" style={{ gap: 6 }}>
            {v.approx ? `≈ #${v.seq}` : `#${v.seq}`}
            {v.seq === agent?.current_seq && <Tag tone="blue">{t("v2.scorecard.currentShort")}</Tag>}
          </span>
        ),
        className: "nowrap",
        render: (line) => {
          const cell = line.cell(v);
          return (
            <MatrixScore
              cell={i === last ? cell : { value: cell.value, delta: null }}
              testId={`v2-assistant-next-breakdown-${v.seq}-${line.key}`}
            />
          );
        },
      }),
    ),
  ];
  return (
    <details className="v2-assistant-breakdown" data-testid="v2-assistant-next-breakdown">
      <summary className="v2-link">{t("assistantNext.run.breakdown", { count: ids.length })}</summary>
      <p className="v2-sc-caption">
        {t("assistantNext.run.breakdownSub")}
        {versions.length > shown.length && <> {t("assistantNext.run.breakdownMore", { count: versions.length - shown.length })}</>}
      </p>
      <Table columns={columns} rows={lines} rowKey={(line) => line.key} density="dense" />
    </details>
  );
}

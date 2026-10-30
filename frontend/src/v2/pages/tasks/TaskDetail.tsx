import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, errorMessage, type V2Range } from "../../../lib/api";
import { coverageInfo, hasInsightTrees, judgeInferenceSummary, runCoverage, type EvaluationRunSessionResults, type ExtractionCheckSummary } from "../../../lib/evaluation";
import { previousComparable } from "../../compare";
import { ExtractionCheckBanner } from "../../ExtractionCheck";
import { UnitCoverageBanner } from "../../UnitCoverage";
import { fmtTime, heartbeatAge, RANGES, rangeLabel } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { InsightClusters } from "../../InsightClusters";
import { EvaluatorBreakdown, ResultsTable, SummaryKpis } from "../../ResultsView";
import { rowsFromOnline, rowsFromRun, summarize } from "../../results";
import { RunCoverageNote, RunCoverageTag } from "../../RunCoverage";
import { agentLabel, sourceLabel, STATUS_TONE, statusLabel, taskFromOnline, taskFromRun, taskItemLabel, type TaskKind, type V2Task } from "../../tasks";
import { useMayMutate } from "../../ownership";
import { runFailureText } from "../../runFailure";
import { Alert, Button, Card, Confirm, Descriptions, FilterSelect, FlowHeader, Spin, Tag } from "../../ui";
import { backState, backTarget, returnPath } from "./back";
import { RunRecommendations } from "./RunRecommendations";
import { useEvaluatorNames } from "../../useEvaluatorNames";
import { EvaluatorName } from "../../EvaluatorName";

const POLL_MS = 8000;

function useTask(kind: TaskKind, id: string) {
  const [tick, setTick] = useState(0);
  const task = useLoad<V2Task>(
    () => (kind === "run" ? api.getEvaluationRun(id).then(taskFromRun) : api.v2OnlineConfig(id).then(taskFromOnline)),
    `task:${kind}:${id}:${tick}`,
  );
  const active = task.data?.status === "running" || task.data?.status === "queued" || task.data?.status === "pending";
  // a batch run moves through queued → evaluating → completed: keep it fresh
  useEffect(() => {
    if (kind !== "run" || !active) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => window.clearInterval(timer);
  }, [kind, active]);
  const reload = useCallback(() => setTick((n) => n + 1), []);
  return { ...task, reload };
}

export function TaskDetail({ kind, id }: { kind: TaskKind; id: string }) {
  const { t } = useTranslation();
  useEvaluatorNames(); // N17-obs429: custom evaluators by name
  const [, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const { can } = useAuth();
  const mayMutate = useMayMutate();
  const task = useTask(kind, id);
  const [range, setRange] = useState<V2Range>("24h");
  const [confirm, setConfirm] = useState<"stop" | "pause" | "resume" | null>(null);
  const [busy, setBusy] = useState(false);
  const data = task.data;
  const terminal = data?.kind === "run" && ["completed", "failed", "stopped"].includes(data.status);
  const insightsRun = data?.kind === "run" && data.mode === "insights";

  const results = useLoad(
    async () => {
      // an insights run carries its clusters on the run row — no judged records to read
      const none = { sessions: [] as EvaluationRunSessionResults[], extraction: null as ExtractionCheckSummary | null };
      if (!data || insightsRun) return { rows: [], note: null as string | null, available: false, ...none };
      if (data.kind === "run") {
        if (!terminal) return { rows: [], note: t("v2.taskDetail.waitResults"), available: false, ...none };
        const res = await api.evaluationRunResults(data.id);
        return {
          rows: rowsFromRun(data, res),
          note: res.available ? (res.truncated ? t("v2.taskDetail.truncated") : null) : t(`v2.taskDetail.reason.${res.reason ?? "unreadable"}`),
          available: res.available,
          sessions: res.sessions,
          extraction: res.extraction_check ?? null,
        };
      }
      const res = await api.v2OnlineResults(data.id, range);
      return {
        rows: rowsFromOnline(data, res.recent),
        note: res.errors.count ? t("v2.taskDetail.onlineErrors", { count: res.errors.count }) : null,
        available: true,
        sessions: [],
        extraction: res.extraction_check ?? null,
      };
    },
    `task-results:${kind}:${id}:${data ? `${data.status}` : "none"}:${range}`,
  );
  const rows = useMemo(() => results.data?.rows ?? [], [results.data]);
  // N12-obs47: reading a pre-summary partial run's results backfills which sessions were not
  // scored (server side): re-read the row once so the per-session note replaces the record count
  const legacyPartial = !!(data?.kind === "run" && data.run && runCoverage(data.run)?.legacy);
  const resultsRead = !!results.data?.available;
  const reloadTask = task.reload;
  useEffect(() => {
    if (legacyPartial && resultsRead) reloadTask();
  }, [legacyPartial, resultsRead, reloadTask]);
  const summary = useMemo(() => summarize(rows), [rows]);
  // eval accuracy item 1: the run payload carries the summary as soon as the run settles;
  // the results read adds the per-session detail (and is the only source for online)
  const extraction = (data?.kind === "run" ? data.run?.extraction_check : null) ?? results.data?.extraction ?? null;
  const flaggedSessions = extraction?.status === "mismatch" ? extraction.mismatch : 0;

  // "compare with the previous run": the newest earlier completed, scored run of the same
  // agent on the same benchmark, read from the ledger-only scorecard. A log-source run
  // has no agent id, so it reads the whole scorecard and matches on its `agent_key`.
  const comparable =
    data?.kind === "run" && data.mode === "evaluators" && data.run?.status === "completed" && data.run.scores.length > 0;
  const previous = useLoad(
    async () => {
      const run = data?.run;
      if (!comparable || !run) return null;
      const card = await api.evaluationScorecard(run.agent_id ? { agent_id: run.agent_id } : {});
      return previousComparable(card.runs, run.id);
    },
    `task-previous:${kind}:${id}:${comparable ? "on" : "off"}`,
  );

  const act = async () => {
    if (!confirm || !data) return;
    setBusy(true);
    try {
      if (confirm === "stop") await api.stopEvaluationRun(data.id);
      else await api.v2OnlineAction(data.id, confirm);
      toast("success", t(`v2.tasks.done.${confirm}`));
      setConfirm(null);
      task.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  if (task.loading && !data) return <Spin />;
  if (task.error && !data) return <Alert tone="error">{task.error}</Alert>;
  if (!data) return null;

  const mayRun = can("eval.run");
  // N17-obs440: an online task's pause / resume are creator-or-admin (server: obs426)
  const onlineLocked = data.kind === "online" && !mayMutate(data.online);
  const onlineHint = onlineLocked ? t("v2.common.creatorOnly") : undefined;
  const previousRun = previous.data;
  const coverage = data.kind === "run" && data.run ? runCoverage(data.run) : null;
  const recheck = async () => {
    if (data.kind !== "run") return;
    setBusy(true);
    try {
      const run = await api.recheckEvaluationRun(data.id);
      toast("success", t(run.status === "failed" ? "v2.tasks.recheckStillFailed" : "v2.tasks.done.recheck"));
      task.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const actions = (
    <>
      {data.kind === "run" && (data.status === "running" || data.status === "queued") && (
        <Button kind="danger" disabled={!mayRun} onClick={() => setConfirm("stop")}>
          {t("v2.tasks.stop")}
        </Button>
      )}
      {data.kind === "run" && data.status === "failed" && data.run?.batch_eval_id && (
        <Button disabled={!mayRun || busy} title={t("v2.tasks.recheckHint")} onClick={() => void recheck()} testId="v2-task-recheck">
          {t("v2.tasks.recheck")}
        </Button>
      )}
      {data.kind === "online" && data.status === "running" && (
        <Button disabled={onlineLocked} title={onlineHint} onClick={() => setConfirm("pause")}>{t("v2.tasks.pause")}</Button>
      )}
      {data.kind === "online" && data.status === "paused" && (
        <Button disabled={!mayRun || onlineLocked} title={onlineHint} onClick={() => setConfirm("resume")}>
          {t("v2.tasks.resume")}
        </Button>
      )}
      {comparable && previousRun && (
        <Button
          title={t("v2.compare.prevHint", { name: previousRun.name || previousRun.id, time: fmtTime(previousRun.created_at) })}
          onClick={() => setParams({ view: "compare", a: previousRun.id, b: data.id })}
          testId="v2-compare-prev"
        >
          {t("v2.compare.prev")}
        </Button>
      )}
      <Button disabled={!mayRun} onClick={() => setParams({ view: "new", from: `${data.kind}:${data.id}` }, { state: backState(location.search) })}>
        {t("v2.tasks.copy")}
      </Button>
      <Button kind="primary" onClick={() => task.reload()}>
        {t("v2.common.refresh")}
      </Button>
    </>
  );

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {data.name}
            {coverage && !coverage.legacy ? <RunCoverageTag coverage={coverage} /> : <Tag tone={STATUS_TONE[data.status]}>{statusLabel(t, data.status)}</Tag>}
          </span>
        }
        onBack={() => {
          const outside = returnPath(location.state);
          if (outside) navigate(outside);
          else setParams(backTarget(location.state));
        }}
        end={actions}
      />
      {/* N17-obs418: a running run shows the worker's newest heartbeat, so a slow one is seen to be alive */}
      {data.kind === "run" && data.status === "running" && data.run?.last_heartbeat_at && (
        <div className="v2-muted" data-testid="v2-run-heartbeat">
          {t("v2.taskDetail.heartbeat", {
            time: fmtTime(data.run.last_heartbeat_at),
            seconds: heartbeatAge(data.run.last_heartbeat_at),
          })}
        </div>
      )}
      {/* N12-obs47: a partially failed run explains WHICH sessions were not scored and why
          (telemetry-only gaps read as info); a row from before the per-session summary keeps the
          N12-bug4 record note below until its results are read or "Read failure details" is clicked */}
      {coverage && data.run && !coverage.legacy && (
        <Alert tone={coverageInfo(coverage) ? "info" : "warn"}>
          <RunCoverageNote coverage={coverage} rawError={data.run.error} />
        </Alert>
      )}
      {/* N12-bug4: a completed run whose evaluator records partly errored states it, translated
          (the server's `error` text says the same thing — shown only when no summary exists) */}
      {data.run?.error && (data.status === "failed" || !(data.run.errors?.count ?? 0)) && !(coverage && !coverage.legacy) && (
        <Alert tone={data.status === "failed" ? "error" : "warn"}>
          {/* N17-obs450: the reason in the console language; the raw message on hover */}
          <span title={data.run.error} data-testid="v2-task-run-error">{runFailureText(data.run, t)}</span>
        </Alert>
      )}
      {data.status !== "failed" && (data.run?.errors?.count ?? 0) > 0 && data.run?.errors && !(coverage && !coverage.legacy) && (
        <div data-testid="v2-task-record-errors">
          <Alert tone="warn">
            {t("v2.taskDetail.recordErrors", {
              count: data.run.errors.count,
              total: data.run.errors.total,
              error: `${data.run.errors.first_type}: ${data.run.errors.first_message}`,
            })}
          </Alert>
        </div>
      )}
      {!!data.run?.budget_stops?.length && (
        <div data-testid="v2-task-budget-stops">
          <Alert tone="warn">
            {t("v2.taskDetail.budgetStops", {
              count: data.run.budget_stops.length,
              list: data.run.budget_stops.map((b) => `${b.scenario_id} (${b.stop_reason || b.code})`).join(", "),
            })}
          </Alert>
        </div>
      )}
      {!!data.run?.scenario_failures?.length && (
        <div data-testid="v2-task-scenario-failures">
          <Alert tone="warn">
            {t("v2.taskDetail.scenarioFailures", {
              count: data.run.scenario_failures.length,
              list: data.run.scenario_failures.map((f) => `${f.scenario_id} (${f.code})`).join(", "),
            })}
          </Alert>
        </div>
      )}
      {!!data.run?.tool_name_rewrites?.length && (
        <div data-testid="v2-task-tool-name-rewrites">
          <Alert>
            {t("v2.taskDetail.toolNameRewrites", {
              count: data.run.tool_name_rewrites.length,
              list: data.run.tool_name_rewrites.map((r) => `${r.from} → ${r.to}`).join(", "),
            })}
          </Alert>
        </div>
      )}
      {!!data.run?.warnings?.length && (
        <div data-testid="v2-task-run-warnings">
          <Alert tone="warn">
            {t("v2.taskDetail.toolsNotObserved", {
              count: data.run.warnings.length,
              list: data.run.warnings.map((w) => `${w.scenario_id ?? w.session_id.slice(0, 8)}: ${w.tools.join(", ")}`).join("; "),
            })}
          </Alert>
        </div>
      )}
      {data.run?.dataset_split && data.run.dataset_split !== "all" && (
        <div data-testid="v2-task-dataset-split">
          <Alert>{t(`v2.taskDetail.datasetSplit.${data.run.dataset_split}`)}</Alert>
        </div>
      )}
      {data.online?.failure_reason && <Alert tone="error">{data.online.failure_reason}</Alert>}
      <Card title={t("v2.taskDetail.overview")}>
        <Descriptions
          items={[
            { label: t("v2.tasks.colName"), value: data.name },
            { label: "ID", value: <span className="mono">{data.id}</span> },
            ...(data.logSource
              ? [
                  { label: t("v2.tasks.cw.target"), value: t("v2.tasks.cw.targetCw") },
                  { label: t("v2.tasks.cw.service"), value: <span className="mono">{data.logSource.service_name}</span> },
                  {
                    label: t("v2.tasks.cw.groupsLabel"),
                    value: (
                      <div className="v2-tags">
                        {data.logSource.log_group_names.map((g) => (
                          <Tag key={g} tone="outline">
                            <span className="mono">{g}</span>
                          </Tag>
                        ))}
                      </div>
                    ),
                  },
                ]
              : [{ label: t("v2.tasks.colAgent"), value: <span data-testid="v2-task-agent">{agentLabel(t, data)}</span> }]),
            { label: t("v2.tasks.colSource"), value: <span data-testid="v2-task-source">{sourceLabel(t, data)}</span> },
            // N17-obs424 (D3-c): the dataset revision this run replayed
            ...(data.run?.dataset_revision != null
              ? [{ label: t("v2.taskDetail.datasetRevision"), value: <span data-testid="v2-task-dataset-revision">{t("v2.datasets.revisionValue", { n: data.run.dataset_revision })}</span> }]
              : []),
            {
              label: t("v2.tasks.colStrategy"),
              value: data.kind === "online" ? t("v2.tasks.strategyContinuous") : t("v2.tasks.strategyHistory"),
            },
            ...(insightsRun ? [{ label: t("v2.tasks.mode"), value: t("v2.tasks.modeInsights") }] : []),
            {
              label: insightsRun ? t("evalPage.newRun.insightTypes") : t("v2.tasks.colEvaluators"),
              value: (
                <div className="v2-tags">
                  {data.evaluators.map((e) => (
                    <Tag key={e} tone="outline">
                      {insightsRun ? taskItemLabel(t, data, e) : <EvaluatorName id={e} />}
                    </Tag>
                  ))}
                </div>
              ),
            },
            { label: t("v2.tasks.colCreated"), value: fmtTime(data.createdAt) },
            { label: t("v2.tasks.colUpdated"), value: fmtTime(data.updatedAt) },
            { label: t("v2.common.creator"), value: data.createdByName || "—" },
            ...(data.kind === "run"
              ? [
                  { label: t("v2.taskDetail.sessions"), value: data.run?.session_ids.length ?? 0 },
                  // P3.1 / P3.2: the judge the run scored with and whether temperature 0 applied
                  {
                    label: t("v2.taskDetail.judge"),
                    value: data.run?.judge_inference ? (
                      <span data-testid="v2-task-judge">
                        <span className="mono">{judgeInferenceSummary(data.run.judge_inference, t)}</span>
                        {!data.run.judge_inference.temperature_applied && (
                          <Tag tone="orange" title={t("v2.taskDetail.judgeNonDeterministicHint")}>
                            {t("v2.taskDetail.judgeNonDeterministic")}
                          </Tag>
                        )}
                        {data.run.judge_inference.failure_detection && (
                          // N17-obs449: the insights failure detector runs on its own judge
                          <span className="sub" data-testid="v2-task-failure-detection-judge">
                            {t("v2.taskDetail.failureDetectionJudge", { model: data.run.judge_inference.failure_detection.model_id })}
                          </span>
                        )}
                      </span>
                    ) : (
                      <span className="mono">{data.run?.judge_model_id || "—"}</span>
                    ),
                  },
                  {
                    label: t("v2.taskDetail.queue"),
                    value: data.run?.queue_position ? t("v2.taskDetail.queuePos", { n: data.run.queue_position }) : "—",
                  },
                ]
              : [
                  { label: t("v2.tasks.samplingRate"), value: data.sourceDetail || "—" },
                  { label: t("v2.tasks.sessionTimeout"), value: data.online?.session_timeout_minutes ? t("v2.taskDetail.minutes", { count: data.online.session_timeout_minutes }) : "—" },
                ]),
            { label: t("v2.tasks.description"), value: data.description || "—" },
          ]}
        />
      </Card>

      {insightsRun ? (
        <Card title={t("v2.taskDetail.insightsTitle")}>
          {hasInsightTrees(data.run?.insights) ? (
            <InsightClusters insights={data.run?.insights ?? {}} />
          ) : (
            <Alert>{terminal ? t("v2.taskDetail.insightsEmpty") : t("v2.taskDetail.insightsWait")}</Alert>
          )}
        </Card>
      ) : (
        <>
          <ExtractionCheckBanner summary={extraction} sessions={results.data?.sessions} />
          {data.kind === "run" && <UnitCoverageBanner coverage={data.run?.unit_coverage} tokens={data.run?.token_usage} />}
          {data.kind === "run" && (data.run?.evaluator_versions?.length ?? 0) > 0 && (
            <div className="v2-muted" data-testid="v2-run-provenance">
              {t("v2.taskDetail.evaluatedWith", {
                list: (data.run?.evaluator_versions ?? [])
                  .map((e) => (e.kind === "builtin" ? `${e.name ?? e.id} (${e.version})` : `${e.name ?? e.id} v${e.version ?? "?"}`))
                  .join(" · "),
                release: data.run?.worker_release ?? "—",
              })}
            </div>
          )}
          <SummaryKpis summary={summary} flaggedSessions={flaggedSessions} />
          <EvaluatorBreakdown summary={summary} scores={data.run?.scores} />

          <Card
            title={t("v2.taskDetail.results")}
            end={
              data.kind === "online" ? (
                <FilterSelect
                  label={t("v2.common.timeRange")}
                  value={range}
                  onChange={(v) => setRange(v as V2Range)}
                  options={RANGES.map((r) => ({ value: r, label: rangeLabel(t, r) }))}
                />
              ) : undefined
            }
          >
            {results.data?.note && <Alert tone="warn">{results.data.note}</Alert>}
            <ResultsTable
              rows={rows}
              loading={results.loading}
              error={results.error}
              onRetry={results.reload}
              range={data.kind === "online" ? range : "7d"}
              showTask={false}
              filterable
              exportName={`task-${data.id}`}
            />
          </Card>
        </>
      )}

      {/* keyed on status: the inputs' eligibility flips when the run completes */}
      {data.kind === "run" && data.run && <RunRecommendations key={data.status} run={data.run} />}

      <Confirm
        open={confirm !== null}
        title={confirm ? t(`v2.tasks.confirm.${confirm}Title`) : ""}
        body={confirm ? t(`v2.tasks.confirm.${confirm}Body`, { name: data.name }) : null}
        confirmLabel={confirm ? t(`v2.tasks.confirm.${confirm}Ok`) : ""}
        danger={confirm === "stop"}
        busy={busy}
        onConfirm={() => void act()}
        onClose={() => setConfirm(null)}
      />
    </>
  );
}

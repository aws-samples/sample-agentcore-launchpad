import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, errorMessage } from "../../../lib/api";
import { isBudgetStop } from "../../../lib/budgets";
import { coverageInfo, hasInsightTrees, runCoverage } from "../../../lib/evaluation";
import { BudgetNoticeAlert, BudgetStopAlert } from "../../BudgetBanner";
import { previousComparable } from "../../compare";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { InsightClusters } from "../../InsightClusters";
import { EvaluatorBreakdown, ResultsTable, SummaryKpis } from "../../ResultsView";
import { rowsFromRun, summarize } from "../../results";
import { RunCoverageNote, RunCoverageTag } from "../../RunCoverage";
import { sourceLabel, STATUS_TONE, statusLabel, taskFromRun, taskItemLabel, type V2Task } from "../../tasks";
import { Alert, Button, Card, Confirm, Descriptions, FlowHeader, Spin, Tag } from "../../ui";
import { backState, backTarget, returnPath } from "./back";
import { RunRecommendations } from "./RunRecommendations";

const POLL_MS = 8000;

function useTask(id: string) {
  const [tick, setTick] = useState(0);
  const task = useLoad<V2Task>(() => api.getEvaluationRun(id).then(taskFromRun), `task:run:${id}:${tick}`);
  const active = task.data?.status === "running" || task.data?.status === "queued";
  // a batch run moves through queued → evaluating → completed: keep it fresh
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => window.clearInterval(timer);
  }, [active]);
  const reload = useCallback(() => setTick((n) => n + 1), []);
  return { ...task, reload };
}

/** One batch evaluation run (`?view=detail&kind=run&id=`); an online config's detail is
 *  `OnlineDetail`. */
export function TaskDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const initialOutcome = params.get("outcome") === "error" ? "error" : "";
  const toast = useV2Toast();
  const { can } = useAuth();
  const task = useTask(id);
  const [confirm, setConfirm] = useState<"stop" | null>(null);
  const [busy, setBusy] = useState(false);
  const data = task.data;
  const terminal = !!data && ["completed", "failed", "stopped"].includes(data.status);
  const insightsRun = data?.mode === "insights";

  const results = useLoad(
    async () => {
      // an insights run carries its clusters on the run row — no judged records to read
      if (!data || insightsRun) return { rows: [], note: null as string | null, available: false };
      if (!terminal) return { rows: [], note: t("v2.taskDetail.waitResults"), available: false };
      const res = await api.evaluationRunResults(data.id);
      return {
        rows: rowsFromRun(data, res),
        note: res.available ? (res.truncated ? t("v2.taskDetail.truncated") : null) : t(`v2.taskDetail.reason.${res.reason ?? "unreadable"}`),
        available: res.available,
      };
    },
    `task-results:run:${id}:${data ? `${data.status}` : "none"}`,
  );
  const rows = useMemo(() => results.data?.rows ?? [], [results.data]);
  // reading a pre-summary partial run's results backfills why its sessions were not
  // scored (server side): re-read the row once so the reason replaces the bare count
  const legacyPartial = !!(data?.run && runCoverage(data.run)?.legacy);
  const resultsRead = !!results.data?.available;
  const reloadTask = task.reload;
  useEffect(() => {
    if (legacyPartial && resultsRead) reloadTask();
  }, [legacyPartial, resultsRead, reloadTask]);
  const summary = useMemo(() => summarize(rows), [rows]);

  // "compare with the previous run": the newest earlier completed, scored run of the same
  // agent on the same benchmark, read from the ledger-only scorecard. A log-source run
  // has no agent id, so it reads the whole scorecard and matches on its `agent_key`.
  const comparable = data?.mode === "evaluators" && data.run?.status === "completed" && data.run.scores.length > 0;
  const previous = useLoad(
    async () => {
      const run = data?.run;
      if (!comparable || !run) return null;
      const card = await api.evaluationScorecard(run.agent_id ? { agent_id: run.agent_id } : {});
      return previousComparable(card.runs, run.id);
    },
    `task-previous:run:${id}:${comparable ? "on" : "off"}`,
  );

  const act = async () => {
    if (!confirm || !data) return;
    setBusy(true);
    try {
      await api.stopEvaluationRun(data.id);
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
  const previousRun = previous.data;
  const coverage = data.run ? runCoverage(data.run) : null;
  const recheck = async () => {
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
      {(data.status === "running" || data.status === "queued") && (
        <Button kind="danger" disabled={!mayRun} onClick={() => setConfirm("stop")}>
          {t("v2.tasks.stop")}
        </Button>
      )}
      {data.status === "failed" && data.run?.batch_eval_id && (
        <Button disabled={!mayRun || busy} title={t("v2.tasks.recheckHint")} onClick={() => void recheck()} testId="v2-task-recheck">
          {t("v2.tasks.recheck")}
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
      <Button disabled={!mayRun} onClick={() => setParams({ view: "new", from: `run:${data.id}` }, { state: backState(location.search) })}>
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
            {coverage ? <RunCoverageTag coverage={coverage} /> : <Tag tone={STATUS_TONE[data.status]}>{statusLabel(t, data.status)}</Tag>}
          </span>
        }
        onBack={() => {
          const outside = returnPath(location.state);
          if (outside) navigate(outside);
          else setParams(backTarget(location.state));
        }}
        end={actions}
      />
      {data.run && <BudgetNoticeAlert id={data.run.id} />}
      {data.run && isBudgetStop(data.run) ? (
        <BudgetStopAlert kind="run" testId="v2-task-budget-stop" />
      ) : coverage && data.run ? (
        <Alert tone={coverageInfo(coverage) ? "info" : "warn"}>
          <RunCoverageNote coverage={coverage} rawError={data.run.error}
            onReadDetail={mayRun ? () => void recheck() : undefined} reading={busy} />
        </Alert>
      ) : data.run?.error && <Alert tone={data.status === "failed" ? "error" : "warn"}>{data.run.error}</Alert>}
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
              : [{ label: t("v2.tasks.colAgent"), value: data.agentName }]),
            { label: t("v2.tasks.colSource"), value: sourceLabel(t, data) },
            ...(insightsRun ? [{ label: t("v2.tasks.mode"), value: t("v2.tasks.modeInsights") }] : []),
            {
              label: insightsRun ? t("evalPage.newRun.insightTypes") : t("v2.tasks.colEvaluators"),
              value: (
                <div className="v2-tags">
                  {data.evaluators.map((e) => (
                    <Tag key={e} tone="outline">
                      {taskItemLabel(t, data, e)}
                    </Tag>
                  ))}
                </div>
              ),
            },
            { label: t("v2.tasks.colCreated"), value: fmtTime(data.createdAt) },
            { label: t("v2.tasks.colUpdated"), value: fmtTime(data.updatedAt) },
            { label: t("v2.common.creator"), value: data.createdByName || "—" },
            { label: t("v2.taskDetail.sessions"), value: data.run?.session_ids.length ?? 0 },
            {
              label: t("v2.taskDetail.queue"),
              value: data.run?.queue_position ? t("v2.taskDetail.queuePos", { n: data.run.queue_position }) : "—",
            },
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
          <SummaryKpis summary={summary} />
          <EvaluatorBreakdown summary={summary} />

          <Card title={t("v2.taskDetail.results")}>
            {results.data?.note && <Alert tone="warn">{results.data.note}</Alert>}
            <ResultsTable
              rows={rows}
              loading={results.loading}
              error={results.error}
              onRetry={results.reload}
              range="7d"
              showTask={false}
              filterable
              initialOutcome={initialOutcome}
              exportName={`task-${data.id}`}
            />
          </Card>
        </>
      )}

      {/* keyed on status: the inputs' eligibility flips when the run completes */}
      {data.run && <RunRecommendations key={data.status} run={data.run} />}

      <Confirm
        open={confirm !== null}
        title={confirm ? t(`v2.tasks.confirm.${confirm}Title`) : ""}
        body={confirm ? t(`v2.tasks.confirm.${confirm}Body`, { name: data.name }) : null}
        confirmLabel={confirm ? t(`v2.tasks.confirm.${confirm}Ok`) : ""}
        danger
        busy={busy}
        onConfirm={() => void act()}
        onClose={() => setConfirm(null)}
      />
    </>
  );
}

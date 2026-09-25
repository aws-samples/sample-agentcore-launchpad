import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import {
  api,
  errorMessage,
  type OnlineEvalConfigRow,
  type OnlineEvalReportRow,
  type OnlineEvalResultsEvaluator,
  type OnlineEvalResultsPoint,
  type V2Range,
} from "../../../lib/api";
import { hasInsightTrees } from "../../../lib/evaluation";
import { evaluatorLabel } from "../../../lib/evaluators";
import { fmtTime, normalizedScore, RANGES, rangeLabel, scoreTone } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
import { InsightClusters } from "../../InsightClusters";
import {
  agentLabel,
  canToggle,
  configName,
  filterText,
  insightLabel,
  isEditable,
  isTransient,
  modeOf,
  OWNER_TONE,
  POLL_MS,
  reportActive,
  reportKey,
  reportTone,
  statusTone,
} from "../../online";
import { EvaluatorBreakdown, ResultsTable, SummaryKpis } from "../../ResultsView";
import { rowsFromOnline, summarize } from "../../results";
import { taskFromOnline } from "../../tasks";
import {
  Alert,
  Button,
  Card,
  Confirm,
  Descriptions,
  Drawer,
  FilterSelect,
  FlowHeader,
  LinkButton,
  Pager,
  Spin,
  Table,
  Tag,
} from "../../ui";

function Sparkline({ points, evaluatorId }: { points: OnlineEvalResultsPoint[]; evaluatorId: string }) {
  const values = points.map((p) => p.mean).filter((v): v is number => v != null);
  if (values.length < 2) return <span className="v2-muted">—</span>;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const W = 96;
  const H = 22;
  const d = values
    .map((v, i) => `${i ? "L" : "M"}${((i / (values.length - 1)) * W).toFixed(1)},${(H - 2 - ((v - lo) / (hi - lo || 1)) * (H - 4)).toFixed(1)}`)
    .join(" ");
  const tone = scoreTone(normalizedScore(values[values.length - 1], evaluatorId));
  const color = tone === "good" ? "var(--v2-success)" : tone === "mid" ? "var(--v2-warning)" : "var(--v2-danger)";
  return (
    <svg width={W} height={H} aria-hidden="true">
      <path d={d} fill="none" stroke={color} strokeWidth={1.5} />
    </svg>
  );
}

/** Scores mode: KPIs from the recent records, the server's per-evaluator aggregates and the records. */
function ScoresPanel({ row }: { row: OnlineEvalConfigRow }) {
  const { t } = useTranslation();
  const [range, setRange] = useState<V2Range>("24h");
  const results = useLoad(() => api.v2OnlineResults(row.config_id, range), `online-results:${row.config_id}:${range}`);
  const task = useMemo(() => taskFromOnline(row), [row]);
  const rows = useMemo(() => rowsFromOnline(task, results.data?.recent ?? []), [task, results.data]);
  const summary = useMemo(() => summarize(rows), [rows]);
  const stats = results.data?.evaluators ?? [];
  const errors = results.data?.errors;

  return (
    <>
      <SummaryKpis summary={summary} />
      <EvaluatorBreakdown summary={summary} />
      <Card
        title={t("v2.online.statsTitle")}
        sub={t("v2.online.statsSub")}
        end={
          <FilterSelect
            label={t("v2.common.timeRange")}
            value={range}
            onChange={(v) => setRange(v as V2Range)}
            options={RANGES.map((r) => ({ value: r, label: rangeLabel(t, r) }))}
          />
        }
        testId="v2-online-stats"
      >
        {errors && errors.count > 0 && (
          <Alert tone="warn">
            {t("v2.online.judgeErrors", { count: errors.count })}
            {errors.first_message ? ` ${errors.first_message}` : ""}
          </Alert>
        )}
        <Table
          columns={[
            {
              key: "e",
              title: t("v2.evaluators.colName"),
              render: (e: OnlineEvalResultsEvaluator) => (
                <>
                  {evaluatorLabel(t, e.evaluator_id)}
                  <span className="sub">{e.level ? t(`v2.level.${e.level}`, { defaultValue: e.level }) : ""}</span>
                </>
              ),
            },
            {
              key: "mean",
              title: t("v2.online.colMean"),
              className: "num",
              render: (e: OnlineEvalResultsEvaluator) =>
                e.mean == null ? (
                  <span className="v2-muted">—</span>
                ) : (
                  <span className={`v2-score ${scoreTone(normalizedScore(e.mean, e.evaluator_id))}`}>{e.mean.toFixed(2)}</span>
                ),
            },
            {
              key: "trend",
              title: t("v2.online.colTrend"),
              render: (e: OnlineEvalResultsEvaluator) => (
                <Sparkline points={results.data?.series[e.evaluator_id] ?? []} evaluatorId={e.evaluator_id} />
              ),
            },
            {
              key: "count",
              title: t("v2.online.colCounts"),
              render: (e: OnlineEvalResultsEvaluator) => t("v2.online.counts", { count: e.count, sessions: e.sessions }),
            },
            {
              key: "labels",
              title: t("v2.online.colLabels"),
              render: (e: OnlineEvalResultsEvaluator) => (
                <div className="v2-tags">
                  {Object.entries(e.labels).map(([label, n]) => (
                    <Tag key={label} tone="outline">
                      {label} {n}
                    </Tag>
                  ))}
                </div>
              ),
            },
          ]}
          rows={stats}
          rowKey={(e) => `${e.evaluator_id}:${e.level}`}
          loading={results.loading}
          error={results.error}
          onRetry={results.reload}
          empty={t("v2.online.noResults", { count: row.session_timeout_minutes ?? 15 })}
        />
      </Card>
      <Card title={t("v2.taskDetail.results")}>
        <ResultsTable
          rows={rows}
          loading={results.loading}
          error={results.error}
          onRetry={results.reload}
          range={range}
          showTask={false}
          exportName={`online-${row.config_id}`}
        />
      </Card>
    </>
  );
}

function ReportDrawer({ configId, report, onClose }: { configId: string; report: OnlineEvalReportRow; onClose: () => void }) {
  const { t } = useTranslation();
  const active = reportActive(report);
  // an open report that is still running refreshes along with the list poll
  const detail = useLoad(
    () => (report.batch_id ? api.onlineEvalReport(configId, report.batch_id) : Promise.resolve(null)),
    `online-report:${configId}:${report.batch_id}:${active ? report.status : "done"}`,
  );
  const d = detail.data;
  return (
    <Drawer open title={t("v2.online.reportTitle", { time: fmtTime(report.created_at) })} onClose={onClose} testId="v2-online-report">
      {report.error && <Alert tone="error">{report.error}</Alert>}
      {!report.batch_id ? (
        <Alert>{t("v2.online.reportNoBatch")}</Alert>
      ) : detail.loading && !d ? (
        <Spin />
      ) : detail.error ? (
        <Alert tone="error">{detail.error}</Alert>
      ) : d ? (
        <>
          <Descriptions
            one
            items={[
              { label: t("v2.tasks.colStatus"), value: <Tag tone={reportTone(d.status)}>{d.status ?? "—"}</Tag> },
              { label: t("v2.online.colSessions"), value: d.sessions.total ?? "—" },
              { label: t("v2.online.window"), value: d.time_range ? `${fmtTime(d.time_range.startTime)} → ${fmtTime(d.time_range.endTime)}` : "—" },
            ]}
          />
          {d.error_details.length > 0 && (
            <Alert tone="warn">
              {t("v2.online.reportErrors", { count: d.error_details.length })} {d.error_details.slice(0, 3).join(" · ")}
            </Alert>
          )}
          {hasInsightTrees(d.insights) ? (
            <InsightClusters insights={d.insights} />
          ) : active ? (
            <Alert>{t("v2.online.reportRunning")}</Alert>
          ) : (d.sessions.total ?? 0) === 0 ? (
            <Alert>{t("v2.online.reportNoSessions")}</Alert>
          ) : (
            <Alert>{t("v2.online.reportNoTrees")}</Alert>
          )}
        </>
      ) : null}
    </Drawer>
  );
}

/** Insights mode: scheduled + on-demand reports, each opened in a drawer. */
function ReportsPanel({ row }: { row: OnlineEvalConfigRow }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { can } = useAuth();
  const [tick, setTick] = useState(0);
  const reports = useLoad(() => api.onlineEvalReports(row.config_id), `online-reports:${row.config_id}:${tick}`);
  const [range, setRange] = useState<V2Range>("24h");
  const [busy, setBusy] = useState(false);
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [showUnattributed, setShowUnattributed] = useState(false);
  const list = useMemo(() => reports.data?.reports ?? [], [reports.data]);
  const unattributed = useMemo(() => reports.data?.unattributed ?? [], [reports.data]);
  const anyActive = list.some(reportActive);
  const consoleRunning = list.some((r) => r.origin === "console" && reportActive(r));
  const canRun = isEditable(row) && can("eval.run");

  useEffect(() => {
    if (!anyActive) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => window.clearInterval(timer);
  }, [anyActive]);

  const run = async () => {
    setBusy(true);
    try {
      await api.onlineEvalRunReport(row.config_id, range);
      toast("success", t("v2.online.reportStarted"));
      setTick((n) => n + 1);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const shown = useMemo(() => (showUnattributed ? [...list, ...unattributed] : list), [showUnattributed, list, unattributed]);
  const paged = usePaged(shown, 10);
  const opened = shown.find((r) => reportKey(r) === openKey) ?? null;

  return (
    <Card
      title={t("v2.online.reportsTitle")}
      sub={t("v2.online.reportsSub")}
      testId="v2-online-reports"
    >
      <div className="v2-toolbar">
        <Button onClick={() => setTick((n) => n + 1)}>{t("v2.common.refresh")}</Button>
        {isEditable(row) && (
          <>
            <FilterSelect
              label={t("v2.common.timeRange")}
              value={range}
              onChange={(v) => setRange(v as V2Range)}
              options={RANGES.map((r) => ({ value: r, label: rangeLabel(t, r) }))}
            />
            <Button
              kind="primary"
              disabled={busy || consoleRunning || isTransient(row) || !canRun}
              title={consoleRunning ? t("v2.online.reportPending") : undefined}
              onClick={() => void run()}
              testId="v2-online-run-report"
            >
              {t("v2.online.runReport")}
            </Button>
          </>
        )}
        {unattributed.length > 0 && (
          <div className="end">
            <label className="v2-check">
              <input type="checkbox" checked={showUnattributed} onChange={(e) => setShowUnattributed(e.target.checked)} />
              {t("v2.online.unattributed", { count: unattributed.length })}
            </label>
          </div>
        )}
      </div>
      {reports.data?.aws_unavailable && <Alert tone="warn">{t("v2.online.awsUnavailable")}</Alert>}
      {showUnattributed && <Alert>{t("v2.online.unattributedNote")}</Alert>}
      <Table
        columns={[
          { key: "created", title: t("v2.tasks.colCreated"), className: "nowrap", render: (r: OnlineEvalReportRow) => fmtTime(r.created_at) },
          {
            key: "origin",
            title: t("v2.online.colOrigin"),
            render: (r: OnlineEvalReportRow) => {
              const origin = unattributed.includes(r) ? "unknown" : r.origin;
              return <Tag tone={origin === "console" ? "blue" : origin === "aws_scheduled" ? "green" : "gray"}>{t(`v2.online.origin.${origin}`)}</Tag>;
            },
          },
          { key: "status", title: t("v2.tasks.colStatus"), render: (r: OnlineEvalReportRow) => <Tag tone={reportTone(r.status)}>{r.status ?? "—"}</Tag> },
          {
            key: "sessions",
            title: t("v2.online.colSessions"),
            render: (r: OnlineEvalReportRow) =>
              r.sessions.total == null
                ? "—"
                : t("v2.online.sessionCounts", { done: r.sessions.completed, failed: r.sessions.failed, running: r.sessions.in_progress }),
          },
          {
            key: "insights",
            title: t("v2.online.colInsights"),
            render: (r: OnlineEvalReportRow) =>
              r.insights.length ? r.insights.map((id) => insightLabel(t, id)).join(" · ") : <span className="v2-muted">{t("v2.online.insightsFromConfig")}</span>,
          },
          {
            key: "ops",
            title: t("v2.common.actions"),
            className: "right",
            render: (r: OnlineEvalReportRow) => <LinkButton onClick={() => setOpenKey(reportKey(r))}>{t("v2.common.view")}</LinkButton>,
          },
        ]}
        rows={paged.slice}
        rowKey={(r) => reportKey(r) || String(r.created_at)}
        loading={reports.loading}
        error={reports.error}
        onRetry={() => setTick((n) => n + 1)}
        empty={t("v2.online.noReports")}
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      {opened && <ReportDrawer configId={row.config_id} report={opened} onClose={() => setOpenKey(null)} />}
    </Card>
  );
}

export function OnlineDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { can } = useAuth();
  const mayRun = can("eval.run");
  const [tick, setTick] = useState(0);
  const config = useLoad(() => api.v2OnlineConfig(id), `online-config:${id}:${tick}`);
  const [confirm, setConfirm] = useState<"pause" | "resume" | "delete" | null>(null);
  const [busy, setBusy] = useState(false);
  const row = config.data;
  const transient = row ? isTransient(row) : false;

  useEffect(() => {
    if (!transient) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => window.clearInterval(timer);
  }, [transient]);

  const act = async () => {
    if (!confirm || !row) return;
    setBusy(true);
    try {
      if (confirm === "delete") {
        await api.v2DeleteOnlineConfig(row.config_id);
        toast("success", t("v2.online.deleted", { logGroup: row.results_log_group }));
        setParams({});
        return;
      }
      await api.v2OnlineAction(row.config_id, confirm);
      toast("success", t(`v2.tasks.done.${confirm}`));
      setConfirm(null);
      setTick((n) => n + 1);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  if (config.loading && !row) return <Spin />;
  if (config.error && !row) {
    return (
      <>
        <FlowHeader title={id} onBack={() => setParams({})} />
        <Alert tone="error">{config.error}</Alert>
      </>
    );
  }
  if (!row) return null;
  const mode = modeOf(row);

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {configName(row)}
            <Tag tone={statusTone(row.status)}>{row.status ?? "—"}</Tag>
            <Tag tone={row.execution_status === "ENABLED" ? "green" : "gray"} dot>
              {t(`v2.online.exec.${row.execution_status === "ENABLED" ? "on" : "off"}`)}
            </Tag>
          </span>
        }
        onBack={() => setParams({})}
        end={
          <>
            <Button onClick={() => setTick((n) => n + 1)}>{t("v2.common.refresh")}</Button>
            {canToggle(row) && row.execution_status === "ENABLED" && (
              <Button disabled={busy || transient} onClick={() => setConfirm("pause")}>
                {t("v2.tasks.pause")}
              </Button>
            )}
            {canToggle(row) && row.execution_status === "DISABLED" && (
              <Button disabled={busy || transient || !mayRun} onClick={() => setConfirm("resume")}>
                {t("v2.tasks.resume")}
              </Button>
            )}
            {canToggle(row) && (
              <Button kind="danger" disabled={busy || row.status === "DELETING" || !mayRun} onClick={() => setConfirm("delete")}>
                {t("v2.common.delete")}
              </Button>
            )}
            {isEditable(row) && (
              <Button
                kind="primary"
                disabled={busy || transient || !mayRun}
                onClick={() => setParams({ view: "edit", id: row.config_id })}
                testId="v2-online-edit"
              >
                {t("v2.common.edit")}
              </Button>
            )}
          </>
        }
      />
      {row.failure_reason && <Alert tone="error">{row.failure_reason}</Alert>}
      {row.duplicate_enabled && <Alert tone="warn">{t("v2.online.duplicateWarn")}</Alert>}
      {row.owner === "experiment" && (
        <Alert action={<Link to="/v2/eval/experiments" className="v2-link">{t("v2.online.openExperiments")}</Link>}>
          {t("v2.online.experimentReadonly")}
        </Alert>
      )}
      {row.owner === "external" && (
        <Alert>
          {row.matched_agent ? t("v2.online.externalMatched", { name: row.matched_agent.name }) : t("v2.online.externalReadonly")}
        </Alert>
      )}
      <Card title={t("v2.taskDetail.overview")} testId="v2-online-overview">
        <Descriptions
          items={[
            { label: t("v2.tasks.colName"), value: configName(row) },
            { label: "ID", value: <span className="mono">{row.config_id}</span> },
            { label: t("v2.tasks.colAgent"), value: agentLabel(row) },
            { label: t("v2.online.colOwner"), value: <Tag tone={OWNER_TONE[row.owner]}>{t(`v2.online.owner.${row.owner}`)}</Tag> },
            { label: t("v2.online.colMode"), value: t(`v2.online.mode.${mode}`) },
            {
              label: mode === "insights" ? t("v2.online.insightTypes") : t("v2.tasks.colEvaluators"),
              value: (
                <div className="v2-tags">
                  {(mode === "insights" ? row.insights : row.evaluators).map((e) => (
                    <Tag key={e} tone="outline">
                      {mode === "insights" ? insightLabel(t, e) : evaluatorLabel(t, e)}
                    </Tag>
                  ))}
                </div>
              ),
            },
            ...(mode === "insights"
              ? [
                  {
                    label: t("v2.online.frequencies"),
                    value: row.clustering_frequencies.length
                      ? row.clustering_frequencies.map((f) => t(`v2.online.freq.${f}`)).join(" · ")
                      : t("v2.online.onDemandOnly"),
                  },
                ]
              : []),
            { label: t("v2.tasks.samplingRate"), value: row.sampling_percentage ?? "—" },
            {
              label: t("v2.tasks.sessionTimeout"),
              value: row.session_timeout_minutes ? t("v2.taskDetail.minutes", { count: row.session_timeout_minutes }) : "—",
            },
            { label: t("v2.online.filters"), value: row.filters.length ? row.filters.map(filterText).join(" · ") : t("v2.online.noFilters") },
            { label: t("v2.online.serviceName"), value: row.data_source.service_name ?? "—" },
            { label: t("v2.online.logGroups"), value: <span className="mono">{row.data_source.log_groups.join(", ") || "—"}</span> },
            { label: t("v2.online.resultsLogGroup"), value: <span className="mono">{row.results_log_group}</span> },
            { label: t("v2.tasks.colCreated"), value: fmtTime(row.created_at) },
            { label: t("v2.tasks.colUpdated"), value: fmtTime(row.updated_at) },
            { label: t("v2.tasks.description"), value: row.description || "—" },
          ]}
        />
        <div style={{ marginTop: 16 }}>
          <Alert>{t(mode === "insights" ? "v2.online.reportsHint" : "v2.online.firstResultsHint", { count: row.session_timeout_minutes ?? 15 })}</Alert>
        </div>
      </Card>
      {mode === "insights" ? <ReportsPanel row={row} /> : <ScoresPanel row={row} />}
      <Confirm
        open={confirm !== null}
        title={confirm ? t(`v2.online.confirm.${confirm}Title`) : ""}
        body={confirm ? t(`v2.online.confirm.${confirm}Body`, { name: configName(row), logGroup: row.results_log_group }) : null}
        confirmLabel={confirm ? t(`v2.online.confirm.${confirm}Ok`) : ""}
        danger={confirm === "delete"}
        busy={busy}
        onConfirm={() => void act()}
        onClose={() => setConfirm(null)}
      />
    </>
  );
}

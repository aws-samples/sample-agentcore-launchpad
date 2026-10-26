import "../online.css";

import { ArrowLeft, Copy, Pause, Play, RefreshCw, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { api, type OnlineEvalConfigRow, type OnlineEvalReportRow, type V2Range } from "../../../lib/api";
import { hasInsightTrees } from "../../../lib/evaluation";
import { evaluatorLabel } from "../../../lib/evaluators";
import { normalizedScore, RANGES, rangeLabel } from "../../../v2/format";
import { InsightClusters } from "../../../v2/InsightClusters";
import {
  agentLabel,
  canToggle,
  configName,
  filterText,
  insightLabel,
  isEditable,
  isTransient,
  POLL_MS,
  reportActive,
  reportKey,
} from "../../../v2/online";
import { ResultsTable } from "../../../v2/ResultsView";
import { rowsFromOnline, summarize } from "../../../v2/results";
import { taskFromOnline } from "../../../v2/tasks";
import { V2ToastProvider } from "../../../v2/ui";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { permReason } from "../../reasons";
import { Btn, Chip, Dialog, Empty, Filters, Lamp, Notice, PageHead, Panel, Skeleton, Spark, Stat } from "../../ui";
import { OnlineCostCard } from "../tasks/CostCard";
import { copyUrl } from "../tasks/model";
import { onlineHref, onlineList, useOnlineAction } from "./actions";
import { configSignal, modeOf, reportSignal, scoreSignal } from "./common";

/** A V2 results/insights component, on the V3 theme (it is not rebuilt yet). */
function Hosted({ children }: { children: React.ReactNode }) {
  return (
    <V2ToastProvider>
      <div className="v2 v3-host">{children}</div>
    </V2ToastProvider>
  );
}

function RangePick({ value, onChange }: { value: V2Range; onChange: (r: V2Range) => void }) {
  const { t } = useTranslation();
  return <Filters value={value} onChange={onChange} options={RANGES.map((r) => ({ value: r, label: rangeLabel(t, r) }))} />;
}

/** Scores mode: the recent records' verdicts, the server's per-evaluator aggregates and the records. */
function ScoresPanel({ row }: { row: OnlineEvalConfigRow }) {
  const { t } = useTranslation();
  const [range, setRange] = useState<V2Range>("24h");
  const results = useLoad(() => api.v2OnlineResults(row.config_id, range), `v3-online-results:${row.config_id}:${range}`);
  const task = useMemo(() => taskFromOnline(row), [row]);
  const rows = useMemo(() => rowsFromOnline(task, results.data?.recent ?? []), [task, results.data]);
  const summary = useMemo(() => summarize(rows), [rows]);
  const stats = results.data?.evaluators ?? [];
  const errors = results.data?.errors;
  const judged = summary.total - summary.errors;

  return (
    <>
      <div className="v3-grid c4">
        <Panel>
          <Stat label={t("v3.online.judged")} value={summary.total}
            foot={t("v2.insights.kpiCountSub", { passed: summary.passed, failed: summary.failed, errors: summary.errors })} />
        </Panel>
        <Panel signal={summary.mean == null ? undefined : scoreSignal(summary.mean)}>
          <Stat label={t("v3.online.mean")} value={summary.mean == null ? "—" : summary.mean.toFixed(2)}
            signal={summary.mean == null ? undefined : scoreSignal(summary.mean)} foot={t("v2.insights.kpiMeanSub")} />
        </Panel>
        <Panel signal={summary.failed ? "act" : undefined}>
          <Stat label={t("v3.online.badCases")} value={summary.failed} signal={summary.failed ? "act" : undefined} foot={t("v2.insights.kpiBadSub")} />
        </Panel>
        <Panel>
          <Stat label={t("v3.online.passRate")} value={judged > 0 ? `${Math.round((summary.passed / judged) * 100)}%` : "—"} foot={t("v2.insights.kpiPassRateSub")} />
        </Panel>
      </div>

      <Panel title={t("v3.online.byEvaluator")} flush end={<RangePick value={range} onChange={setRange} />}>
        {errors && errors.count > 0 && (
          <div style={{ padding: "0 20px 12px" }}>
            <Notice s="wait">
              {t("v2.online.judgeErrors", { count: errors.count })}
              {errors.first_message ? ` ${errors.first_message}` : ""}
            </Notice>
          </div>
        )}
        {results.loading && !results.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={3} /></div>
        ) : results.error ? (
          <div style={{ padding: 20 }}><Notice s="act"><ApiErrorDetail error={results.failure ?? results.error} /></Notice></div>
        ) : stats.length === 0 ? (
          <Empty title={t("v2.online.noResults", { count: row.session_timeout_minutes ?? 15 })} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v3.online.evaluator")}</th>
                <th className="num">{t("v3.online.mean")}</th>
                <th>{t("v3.online.trend")}</th>
                <th className="num">{t("v3.online.counts")}</th>
                <th>{t("v3.online.labels")}</th>
              </tr>
            </thead>
            <tbody>
              {stats.map((e) => {
                const norm = e.mean == null ? null : normalizedScore(e.mean, e.evaluator_id);
                const series = (results.data?.series[e.evaluator_id] ?? [])
                  .map((p) => p.mean)
                  .filter((v): v is number => v != null)
                  .map((v) => normalizedScore(v, e.evaluator_id));
                const s = scoreSignal(norm);
                return (
                  <tr key={`${e.evaluator_id}:${e.level}`}>
                    <td style={{ width: 30 }}><Lamp s={s} /></td>
                    <td>
                      <div className="v3-name">
                        <div>
                          <b>{evaluatorLabel(t, e.evaluator_id)}</b>
                          <small>{e.level ? t(`v2.level.${e.level}`, { defaultValue: e.level }) : e.evaluator_id}</small>
                        </div>
                      </div>
                    </td>
                    <td className="num"><span className="v3-onl-score" data-s={s}>{e.mean == null ? "—" : e.mean.toFixed(2)}</span></td>
                    <td style={{ width: 140 }}><div className="v3-onl-spark">{series.length > 1 ? <Spark values={series} s={s === "act" ? "act" : "ok"} /> : <span style={{ color: "var(--v3-text-3)" }}>—</span>}</div></td>
                    <td className="num">{t("v2.online.counts", { count: e.count, sessions: e.sessions })}</td>
                    <td>
                      <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                        {Object.entries(e.labels).map(([label, n]) => <Chip key={label}>{label} {n}</Chip>)}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>

      <Panel title={t("v3.online.records")}>
        <Hosted>
          <ResultsTable rows={rows} loading={results.loading} error={results.error} onRetry={results.reload}
            range={range} showTask={false} exportName={`online-${row.config_id}`} />
        </Hosted>
      </Panel>
    </>
  );
}

function ReportDialog({ configId, report, onClose }: { configId: string; report: OnlineEvalReportRow; onClose: () => void }) {
  const { t } = useTranslation();
  const active = reportActive(report);
  // a report still running refreshes along with the list poll
  const detail = useLoad(
    () => (report.batch_id ? api.onlineEvalReport(configId, report.batch_id) : Promise.resolve(null)),
    `v3-online-report:${configId}:${report.batch_id}:${active ? report.status : "done"}`,
  );
  const d = detail.data;
  return (
    <Dialog wide title={t("v2.online.reportTitle", { time: ago(report.created_at) })} onClose={onClose}
      foot={<Btn kind="ghost" onClick={onClose}>{t("v3.online.close")}</Btn>}>
      <div style={{ display: "grid", gap: 12, maxHeight: "62vh", overflow: "auto" }}>
        {report.error && <Notice s="act">{report.error}</Notice>}
        {!report.batch_id ? (
          <Notice>{t("v2.online.reportNoBatch")}</Notice>
        ) : detail.loading && !d ? (
          <Skeleton rows={3} />
        ) : detail.error ? (
          <Notice s="act"><ApiErrorDetail error={detail.failure ?? detail.error} /></Notice>
        ) : d ? (
          <>
            <dl className="v3-kv">
              <dt>{t("v3.online.status")}</dt>
              <dd><Chip s={reportSignal(d.status)}>{d.status ?? "—"}</Chip></dd>
              <dt>{t("v3.online.sessions")}</dt>
              <dd>{d.sessions.total ?? "—"}</dd>
              <dt>{t("v3.online.window")}</dt>
              <dd className="mono">{d.time_range ? `${d.time_range.startTime} → ${d.time_range.endTime}` : "—"}</dd>
            </dl>
            {d.error_details.length > 0 && (
              <Notice s="wait">{t("v2.online.reportErrors", { count: d.error_details.length })} {d.error_details.slice(0, 3).join(" · ")}</Notice>
            )}
            {hasInsightTrees(d.insights) ? (
              <Hosted><InsightClusters insights={d.insights} /></Hosted>
            ) : active ? (
              <Notice s="wait">{t("v2.online.reportRunning")}</Notice>
            ) : (d.sessions.total ?? 0) === 0 ? (
              <Notice>{t("v2.online.reportNoSessions")}</Notice>
            ) : (
              <Notice>{t("v2.online.reportNoTrees")}</Notice>
            )}
          </>
        ) : null}
      </div>
    </Dialog>
  );
}

/** Insights mode: scheduled and on-demand reports, each opened in a dialog. */
function ReportsPanel({ row }: { row: OnlineEvalConfigRow }) {
  const { t } = useTranslation();
  const toast = useToast();
  const { can } = useAuth();
  const [tick, setTick] = useState(0);
  const reports = useLoad(() => api.onlineEvalReports(row.config_id), `v3-online-reports:${row.config_id}:${tick}`);
  const [range, setRange] = useState<V2Range>("24h");
  const [busy, setBusy] = useState(false);
  // a failed "run report" stays on the panel with its AWS details (projects §19)
  const [runError, setRunError] = useState<unknown>(null);
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [showUnattributed, setShowUnattributed] = useState(false);
  const [limit, setLimit] = useState(10);
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
    setRunError(null);
    try {
      await api.onlineEvalRunReport(row.config_id, range);
      toast("ok", t("v2.online.reportStarted"));
      setTick((n) => n + 1);
    } catch (err) {
      setRunError(err);
    } finally {
      setBusy(false);
    }
  };

  const shown = useMemo(() => (showUnattributed ? [...list, ...unattributed] : list), [showUnattributed, list, unattributed]);
  const opened = shown.find((r) => reportKey(r) === openKey) ?? null;

  return (
    <Panel
      title={t("v3.online.reports")}
      flush
      end={
        <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
          <Btn size="sm" kind="ghost" onClick={() => setTick((n) => n + 1)}><RefreshCw size={13} /></Btn>
          {isEditable(row) && (
            <>
              <RangePick value={range} onChange={setRange} />
              <Btn size="sm" kind="primary" busy={busy} disabled={consoleRunning || isTransient(row) || !canRun}
                disabledReason={!canRun ? permReason(t, "eval.run") : isTransient(row) ? t("story.reason.settling") : t("v2.online.reportPending")}
                onClick={() => void run()}>
                {t("v2.online.runReport")}
              </Btn>
            </>
          )}
        </span>
      }
    >
      <div style={{ padding: "0 20px 12px", display: "grid", gap: 10 }}>
        <p style={{ margin: 0, color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.online.reportsSub")}</p>
        {runError != null && <Notice s="act"><ApiErrorDetail error={runError} /></Notice>}
        {reports.data?.aws_unavailable && <Notice s="wait">{t("v2.online.awsUnavailable")}</Notice>}
        {unattributed.length > 0 && (
          <label className="v3-onl-check">
            <input type="checkbox" checked={showUnattributed} onChange={(e) => setShowUnattributed(e.target.checked)} />
            {t("v2.online.unattributed", { count: unattributed.length })}
          </label>
        )}
        {showUnattributed && <Notice>{t("v2.online.unattributedNote")}</Notice>}
      </div>
      {reports.loading && !reports.data ? (
        <div style={{ padding: 20 }}><Skeleton rows={3} /></div>
      ) : reports.error ? (
        <div style={{ padding: 20 }}><Notice s="act"><ApiErrorDetail error={reports.failure ?? reports.error} /></Notice></div>
      ) : shown.length === 0 ? (
        <Empty title={t("v2.online.noReports")} />
      ) : (
        <>
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v3.online.created")}</th>
                <th>{t("v3.online.origin")}</th>
                <th>{t("v3.online.status")}</th>
                <th>{t("v3.online.sessions")}</th>
                <th>{t("v3.online.insights")}</th>
              </tr>
            </thead>
            <tbody>
              {shown.slice(0, limit).map((r) => {
                const origin = unattributed.includes(r) ? "unknown" : r.origin;
                const s = reportSignal(r.status);
                return (
                  <tr key={reportKey(r) || String(r.created_at)} className="click" onClick={() => setOpenKey(reportKey(r))}>
                    <td style={{ width: 30 }}><Lamp s={s} live={reportActive(r)} /></td>
                    <td className="mono">{ago(r.created_at)}</td>
                    <td><Chip s={origin === "console" ? "info" : origin === "aws_scheduled" ? "ok" : undefined}>{t(`v2.online.origin.${origin}`)}</Chip></td>
                    <td><Chip s={s === "info" ? undefined : s}>{r.status ?? "—"}</Chip></td>
                    <td style={{ color: "var(--v3-text-2)" }}>
                      {r.sessions.total == null ? "—" : t("v2.online.sessionCounts", { done: r.sessions.completed, failed: r.sessions.failed, running: r.sessions.in_progress })}
                    </td>
                    <td style={{ color: "var(--v3-text-2)" }}>
                      {r.insights.length ? r.insights.map((id) => insightLabel(t, id)).join(" · ") : t("v2.online.insightsFromConfig")}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {shown.length > limit && (
            <div style={{ padding: 14, display: "flex", justifyContent: "center" }}>
              <Btn size="sm" onClick={() => setLimit((n) => n + 10)}>{t("v3.online.more", { count: shown.length - limit })}</Btn>
            </div>
          )}
        </>
      )}
      {opened && <ReportDialog configId={row.config_id} report={opened} onClose={() => setOpenKey(null)} />}
    </Panel>
  );
}

export function OnlineDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { can } = useAuth();
  const mayRun = can("eval.run");
  const [tick, setTick] = useState(0);
  const config = useLoad(() => api.v2OnlineConfig(id), `v3-online-config:${id}:${tick}`);
  const action = useOnlineAction((done) => (done === "delete" ? navigate(onlineList) : setTick((n) => n + 1)));
  const row = config.data;
  const transient = row ? isTransient(row) : false;

  useEffect(() => {
    if (!transient) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => window.clearInterval(timer);
  }, [transient]);

  if (config.loading && !row) return <Skeleton rows={6} />;
  if (config.error && !row) return <Notice s="act"><ApiErrorDetail error={config.failure ?? config.error} /></Notice>;
  if (!row) return null;
  const mode = modeOf(row);
  const s = configSignal(row);
  const judges = mode === "insights" ? row.insights : row.evaluators;

  return (
    <div className="v3-reveal v3-onl" style={{ display: "grid", gap: 16 }}>
      <div>
        <button type="button" className="v3-btn ghost sm" onClick={() => navigate(onlineList)}>
          <ArrowLeft size={14} /> {t("v2.scorecard.tabs.online")}
        </button>
      </div>
      <PageHead
        eyebrow={`${t(`v2.online.mode.${mode}`)} · ${t(`v2.online.owner.${row.owner}`)} · ${row.config_id}`}
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 14 }}>
            <Lamp s={s} live={s === "ok" || s === "wait"} />
            {configName(row)}
          </span>
        }
        sub={row.description || agentLabel(row)}
        end={
          <>
            <Btn kind="ghost" onClick={() => setTick((n) => n + 1)}><RefreshCw size={14} /></Btn>
            {canToggle(row) && row.execution_status === "ENABLED" && (
              <Btn busy={action.busy} disabled={transient} disabledReason={t("story.reason.settling")} onClick={() => action.ask({ row, action: "pause" })}><Pause size={14} /> {t("v2.tasks.pause")}</Btn>
            )}
            {canToggle(row) && row.execution_status === "DISABLED" && (
              <Btn busy={action.busy} disabled={transient || !mayRun}
                disabledReason={!mayRun ? permReason(t, "eval.run") : t("story.reason.settling")} onClick={() => action.ask({ row, action: "resume" })}><Play size={14} /> {t("v2.tasks.resume")}</Btn>
            )}
            {canToggle(row) && (
              <Btn kind="danger" busy={action.busy} disabled={row.status === "DELETING" || !mayRun}
                disabledReason={!mayRun ? permReason(t, "eval.run") : t("story.reason.deleting")} onClick={() => action.ask({ row, action: "delete" })}>
                <Trash2 size={14} /> {t("v2.common.delete")}
              </Btn>
            )}
            {/* a copy opens the continuous create form seeded from this config (as V2) */}
            {isEditable(row) && (
              <Btn disabled={!mayRun} disabledReason={permReason(t, "eval.run")}
                onClick={() => navigate(copyUrl({ kind: "online", id: row.config_id }))}>
                <Copy size={14} /> {t("v2.tasks.copy")}
              </Btn>
            )}
            {isEditable(row) && (
              <Btn kind="primary" busy={action.busy} disabled={transient || !mayRun}
                disabledReason={!mayRun ? permReason(t, "eval.run") : t("story.reason.settling")} onClick={() => navigate(onlineHref({ view: "edit", id: row.config_id }))}>
                {t("v3.online.edit")}
              </Btn>
            )}
          </>
        }
      />
      {row.failure_reason && <Notice s="act">{row.failure_reason}</Notice>}
      {row.duplicate_enabled && <Notice s="wait">{t("v2.online.duplicateWarn")}</Notice>}
      {row.owner === "experiment" && (
        <Notice>
          {t("v2.online.experimentReadonly")}{" "}
          <Link to="/v2/eval/experiments" style={{ textDecoration: "underline" }}>{t("v2.online.openExperiments")}</Link>
        </Notice>
      )}
      {row.owner === "external" && (
        <Notice>{row.matched_agent ? t("v2.online.externalMatched", { name: row.matched_agent.name }) : t("v2.online.externalReadonly")}</Notice>
      )}

      <div className="v3-grid c4">
        <Panel signal={s === "off" ? undefined : s}>
          <Stat label={t("v3.online.state")} value={row.status && row.status !== "ACTIVE" ? row.status : t(`v2.online.exec.${row.execution_status === "ENABLED" ? "on" : "off"}`)} signal={s === "off" ? undefined : s} />
        </Panel>
        <Panel><Stat label={t("v3.online.sampling")} value={row.sampling_percentage != null ? `${row.sampling_percentage}%` : "—"} foot={t("v3.online.samplingFoot")} /></Panel>
        <Panel><Stat label={t("v3.online.timeout")} value={row.session_timeout_minutes ?? "—"} unit={row.session_timeout_minutes ? t("v3.online.minutes") : undefined} foot={t("v3.online.timeoutFoot")} /></Panel>
        <Panel><Stat label={mode === "insights" ? t("v2.online.insightTypes") : t("v3.online.judges")} value={judges.length} foot={agentLabel(row)} /></Panel>
      </div>

      {/* eval-cost §1.3: what an agent-owned config costs per day (judging + its reports) */}
      {row.owner === "agent" && <OnlineCostCard configId={row.config_id} />}

      <Panel title={t("v3.online.setup")}>
        <dl className="v3-kv">
          <dt>{mode === "insights" ? t("v2.online.insightTypes") : t("v2.tasks.colEvaluators")}</dt>
          <dd style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {judges.length ? judges.map((e) => <Chip key={e}>{mode === "insights" ? insightLabel(t, e) : evaluatorLabel(t, e)}</Chip>) : "—"}
          </dd>
          {mode === "insights" && (
            <>
              <dt>{t("v2.online.frequencies")}</dt>
              <dd>{row.clustering_frequencies.length ? row.clustering_frequencies.map((f) => t(`v2.online.freq.${f}`)).join(" · ") : t("v2.online.onDemandOnly")}</dd>
            </>
          )}
          <dt>{t("v2.online.filters")}</dt>
          <dd className="mono">{row.filters.length ? row.filters.map(filterText).join(" · ") : t("v2.online.noFilters")}</dd>
          <dt>{t("v2.online.serviceName")}</dt>
          <dd className="mono">{row.data_source.service_name ?? "—"}</dd>
          <dt>{t("v2.online.logGroups")}</dt>
          <dd className="mono">{row.data_source.log_groups.join(", ") || "—"}</dd>
          <dt>{t("v2.online.resultsLogGroup")}</dt>
          <dd className="mono">{row.results_log_group}</dd>
          <dt>{t("v3.online.created")}</dt>
          <dd>{ago(row.created_at)} · {t("v3.online.updated")} {ago(row.updated_at)}</dd>
        </dl>
        <div style={{ marginTop: 14 }}>
          <Notice>{t(mode === "insights" ? "v2.online.reportsHint" : "v2.online.firstResultsHint", { count: row.session_timeout_minutes ?? 15 })}</Notice>
        </div>
      </Panel>

      {mode === "insights" ? <ReportsPanel row={row} /> : <ScoresPanel row={row} />}
      {action.dialog}
    </div>
  );
}

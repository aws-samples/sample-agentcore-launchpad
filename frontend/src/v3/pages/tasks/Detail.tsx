import {
  ArrowLeft,
  Copy,
  DatabaseZap,
  Download,
  RefreshCw,
  RotateCw,
  Square,
  Tag,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { useAuth } from "../../../auth/auth-context";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { api, type V2Range } from "../../../lib/api";
import { calibrationApi } from "../../../lib/api/calibration";
import { hasInsightTrees } from "../../../lib/evaluation";
import { evaluatorLabel } from "../../../lib/evaluators";
import { downloadCsv, fmtTime } from "../../../v2/format";
import { InsightClusters } from "../../../v2/InsightClusters";
import { AddToDatasetModal } from "../../../v2/pages/data/AddToDataset";
import { RunRecommendations } from "../../../v2/pages/tasks/RunRecommendations";
import {
  type ResultRow,
  rowsFromRun,
  summarize,
} from "../../../v2/results";
import {
  sourceLabel,
  statusLabel,
  taskFromRun,
  taskItemLabel,
  type V2Task,
} from "../../../v2/tasks";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { useNextStep } from "../../story/useNextStep";
import {
  Btn,
  Chip,
  Confirm,
  Dialog,
  Empty,
  Filters,
  Lamp,
  Notice,
  PageHead,
  Panel,
  Skeleton,
  Stat,
  Track,
  type TrackNode,
} from "../../ui";
import {
  calibratableCriteria,
  calibrateBlocker,
  calibrateUrl,
  soleCriterion,
} from "../standards/calibrationLink";
import { AtShell, HostedV2, ScoreChip } from "./common";
import { CopyId } from "./CopyId";
import { RunCostCard } from "./CostCard";
import { copyUrl, outcomeSignal, scoreSignal, taskSignal } from "./model";
import { permReason } from "../../reasons";

const POLL_MS = 8000;
const RESULTS_PAGE = 20;

function useTask(id: string) {
  const [tick, setTick] = useState(0);
  const task = useLoad<V2Task>(
    () => api.getEvaluationRun(id).then(taskFromRun),
    `v3-task:run:${id}:${tick}`,
  );
  const active =
    task.data?.status === "running" ||
    task.data?.status === "queued" ||
    task.data?.status === "pending";
  // a batch run moves through queued → evaluating → completed: keep it fresh (as V2)
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => window.clearInterval(timer);
  }, [active]);
  return { ...task, reload: () => setTick((n) => n + 1) };
}

/** queued → invoke → wait for telemetry → evaluate → done, read off the raw run status. */
function runTrack(task: V2Task, t: (k: string) => string): TrackNode[] {
  const raw = task.run?.status ?? "queued";
  const steps = [
    "queued",
    "invoking",
    "waiting",
    "evaluating",
    "completed",
  ] as const;
  const terminalBad = raw === "failed" || raw === "stopped";
  // a failed/stopped run stopped somewhere: light what it certainly passed
  const at = terminalBad
    ? 4
    : Math.max(0, steps.indexOf(raw as (typeof steps)[number]));
  return steps.map((step, i) => {
    const last = i === steps.length - 1;
    if (last && terminalBad) {
      return {
        key: raw,
        label: statusLabel(t as never, task.status),
        s: raw === "failed" ? "act" : "off",
        here: true,
      };
    }
    return {
      key: step,
      label: t(`v3.tasks.step.${step}`),
      s:
        i < at
          ? "ok"
          : i === at
            ? step === "completed"
              ? "ok"
              : "info"
            : "off",
      here: i === at,
    };
  });
}

function ResultDialog({
  row,
  range,
  onClose,
}: {
  row: ResultRow;
  range: V2Range;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const session = useLoad(
    () =>
      row.sessionId
        ? api.obsSessionTranscript(row.sessionId, row.agentId)
        : Promise.resolve(null),
    `v3-result-transcript:${row.sessionId}:${row.agentId ?? ""}`,
  );
  const [adding, setAdding] = useState(false);
  // Esc closes it wherever focus is (the shared Dialog only hears keys inside it)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !adding && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [adding, onClose]);
  const turns = session.data?.transcript.turns ?? [];
  return (
    <>
      <Dialog
        wide
        title={t("v2.insights.detailTitle")}
        onClose={onClose}
        foot={
          <>
            <Btn kind="ghost" onClick={onClose}>
              {t("v3.tasks.close")}
            </Btn>
            {row.traceId && (
              <Btn
                kind="ghost"
                onClick={() =>
                  navigate(
                    `/v2/eval/data?tab=traces&view=trace&id=${encodeURIComponent(row.traceId ?? "")}&range=${range}`,
                  )
                }
              >
                {t("v2.insights.openTrace")}
              </Btn>
            )}
            <Btn
              kind="primary"
              disabled={!row.sessionId}
              disabledReason={t("story.reason.noSessionLinked")}
              onClick={() => setAdding(true)}
            >
              <DatabaseZap size={14} /> {t("v2.insights.addBadCase")}
            </Btn>
          </>
        }
      >
        <div
          style={{
            display: "grid",
            gap: 14,
            maxHeight: "60vh",
            overflow: "auto",
          }}
        >
          <dl className="v3-kv">
            <dt>{t("v2.insights.colOutcome")}</dt>
            <dd>
              <Chip s={outcomeSignal(row.outcome)}>
                {t(`v2.outcome.${row.outcome}`)}
              </Chip>
            </dd>
            <dt>{t("v2.insights.colEvaluator")}</dt>
            <dd>{evaluatorLabel(t, row.evaluatorId)}</dd>
            <dt>{t("v2.insights.colRaw")}</dt>
            <dd className="mono">{row.score == null ? "—" : row.score}</dd>
            <dt>{t("v2.insights.colNormalized")}</dt>
            <dd>
              <ScoreChip value={row.normalized} />
            </dd>
            <dt>{t("v2.insights.colLabel")}</dt>
            <dd>{row.label ?? "—"}</dd>
            <dt>{t("v2.traces.colSession")}</dt>
            <dd className="mono">{row.sessionId ?? "—"}</dd>
            <dt>{t("v2.insights.colTime")}</dt>
            <dd>{fmtTime(row.time)}</dd>
          </dl>
          <div>
            <div className="v3-task-subtitle">
              {t("v2.insights.explanation")}
            </div>
            <pre className="v3-pre">{row.error ?? row.explanation ?? "—"}</pre>
          </div>
          <div>
            <div className="v3-task-subtitle">
              {t("v2.insights.inputOutput")}
            </div>
            {!row.sessionId ? (
              <p className="v3-task-muted">{t("v2.traces.noSession")}</p>
            ) : session.loading ? (
              <Skeleton rows={2} />
            ) : turns.length === 0 ? (
              <p className="v3-task-muted">{t("v2.traces.noTranscript")}</p>
            ) : (
              <div style={{ display: "grid", gap: 8 }}>
                {turns.map((turn, i) => (
                  <div
                    key={i}
                    className={
                      turn.role.toLowerCase() === "user"
                        ? "v3-task-turn user"
                        : "v3-task-turn"
                    }
                  >
                    <span className="who">
                      {turn.role.toLowerCase() === "user"
                        ? t("v2.traces.user")
                        : "Agent"}
                    </span>
                    <div className="msg">{turn.text}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </Dialog>
      {adding && (
        <HostedV2>
          <AddToDatasetModal
            open
            sessionIds={row.sessionId ? [row.sessionId] : []}
            range={range}
            onClose={() => setAdding(false)}
          />
        </HostedV2>
      )}
    </>
  );
}

function Results({
  rows,
  loading,
  error,
  onRetry,
  range,
  exportName,
}: {
  rows: ResultRow[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  range: V2Range;
  exportName: string;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState<ResultRow | null>(null);
  const [adding, setAdding] = useState(false);
  const [outcome, setOutcome] = useState<"all" | ResultRow["outcome"]>("all");
  const [shown, setShown] = useState(RESULTS_PAGE);
  // newest first, capped at what one from-sessions call accepts (as V2)
  const badSessions = useMemo(
    () =>
      [
        ...new Set(
          rows
            .filter((r) => r.outcome === "failed" && r.sessionId)
            .map((r) => r.sessionId as string),
        ),
      ].slice(0, 50),
    [rows],
  );
  // Bad Cases lead: they are what the run is for
  const ORDER = { failed: 0, error: 1, passed: 2 } as const;
  const visible = useMemo(
    () =>
      rows
        .filter((r) => outcome === "all" || r.outcome === outcome)
        .sort((a, b) => ORDER[a.outcome] - ORDER[b.outcome]),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- ORDER is a constant
    [rows, outcome],
  );
  const count = (o: ResultRow["outcome"]) =>
    rows.filter((r) => r.outcome === o).length;

  const exportCsv = () =>
    downloadCsv(
      `${exportName}.csv`,
      [
        t("v2.insights.colOutcome"),
        t("v2.insights.colTime"),
        t("v2.insights.colRaw"),
        t("v2.insights.colNormalized"),
        t("v2.insights.colLabel"),
        t("v2.insights.colEvaluator"),
        t("v2.insights.colTask"),
        "Agent",
        t("v2.traces.colSession"),
        t("v2.insights.explanation"),
      ],
      rows.map((r) => [
        t(`v2.outcome.${r.outcome}`),
        fmtTime(r.time),
        r.score,
        r.normalized == null ? null : Number(r.normalized.toFixed(4)),
        r.label,
        evaluatorLabel(t, r.evaluatorId),
        r.taskName,
        r.agent,
        r.sessionId,
        r.error ?? r.explanation,
      ]),
    );

  return (
    <>
      <div
        style={{
          display: "flex",
          gap: 10,
          alignItems: "center",
          flexWrap: "wrap",
          padding: "0 20px 14px",
        }}
      >
        <Filters
          value={outcome}
          onChange={(v) => {
            setOutcome(v);
            setShown(RESULTS_PAGE);
          }}
          options={[
            { value: "all", label: t("v3.tasks.all"), count: rows.length },
            {
              value: "failed",
              label: t("v2.outcome.failed"),
              s: "act",
              count: count("failed"),
            },
            {
              value: "error",
              label: t("v2.outcome.error"),
              s: "wait",
              count: count("error"),
            },
            {
              value: "passed",
              label: t("v2.outcome.passed"),
              s: "ok",
              count: count("passed"),
            },
          ]}
        />
        <span style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <Btn size="sm" disabled={rows.length === 0} disabledReason={t("story.reason.nothingToExport")} onClick={exportCsv}>
            <Download size={13} /> {t("v2.insights.export")}
          </Btn>
          <Btn
            size="sm"
            disabled={badSessions.length === 0}
            disabledReason={t("story.reason.noBadCases")}
            onClick={() => setAdding(true)}
          >
            <DatabaseZap size={13} />{" "}
            {t("v2.insights.badToDataset", { count: badSessions.length })}
          </Btn>
        </span>
      </div>
      {loading && rows.length === 0 ? (
        <div style={{ padding: 20 }}>
          <Skeleton rows={4} />
        </div>
      ) : error ? (
        <div style={{ padding: "0 20px 20px" }}>
          <Notice s="act">
            {error}{" "}
            <Btn size="sm" kind="ghost" onClick={onRetry}>
              {t("v3.tasks.retry")}
            </Btn>
          </Notice>
        </div>
      ) : visible.length === 0 ? (
        <Empty title={t("v2.insights.empty")} />
      ) : (
        <>
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v2.insights.colEvaluator")}</th>
                <th>{t("v2.insights.colNormalized")}</th>
                <th className="num">{t("v2.insights.colRaw")}</th>
                <th>{t("v2.insights.colLabel")}</th>
                <th>{t("v2.insights.explanation")}</th>
                <th>{t("v2.traces.colSession")}</th>
                <th className="num">{t("v2.insights.colTime")}</th>
              </tr>
            </thead>
            <tbody>
              {visible.slice(0, shown).map((r) => (
                <tr key={r.key} className="click" onClick={() => setOpen(r)}>
                  <td style={{ width: 30 }}>
                    <Lamp
                      s={outcomeSignal(r.outcome)}
                      title={t(`v2.outcome.${r.outcome}`)}
                    />
                  </td>
                  <td>{evaluatorLabel(t, r.evaluatorId)}</td>
                  <td>
                    <ScoreChip value={r.normalized} />
                  </td>
                  <td className="num">{r.score == null ? "—" : r.score}</td>
                  <td>{r.label ? <Chip>{r.label}</Chip> : "—"}</td>
                  <td
                    style={{
                      color: r.error ? "var(--v3-act)" : "var(--v3-text-2)",
                    }}
                  >
                    <span
                      className="v3-task-clip2"
                      title={r.error ?? r.explanation ?? ""}
                    >
                      {r.error ?? r.explanation ?? "—"}
                    </span>
                  </td>
                  <td className="mono" style={{ color: "var(--v3-text-3)" }}>
                    {r.sessionId ? r.sessionId.slice(0, 10) : "—"}
                  </td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>
                    {ago(r.time)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {visible.length > shown && (
            <div
              style={{ padding: 14, display: "flex", justifyContent: "center" }}
            >
              <Btn size="sm" onClick={() => setShown((n) => n + RESULTS_PAGE)}>
                {t("v3.tasks.more", { count: visible.length - shown })}
              </Btn>
            </div>
          )}
        </>
      )}
      {open && (
        <AtShell>
          <ResultDialog
            row={open}
            range={range}
            onClose={() => setOpen(null)}
          />
        </AtShell>
      )}
      {adding && (
        <AtShell>
          <HostedV2>
            <AddToDatasetModal
              open
              sessionIds={badSessions}
              range={range}
              onClose={() => setAdding(false)}
            />
          </HostedV2>
        </AtShell>
      )}
    </>
  );
}

/** One batch evaluation run; an online config's page is `online/Detail.tsx`. */
export function TaskDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const { can } = useAuth();
  const task = useTask(id);
  // the window a result's trace is looked up in
  const range: V2Range = "24h";
  const [confirm, setConfirm] = useState<"stop" | null>(null);
  const [busy, setBusy] = useState(false);
  // a failed stop / recheck stays on the page with its way out (storyline-ux §4)
  const [failure, setFailure] = useState<unknown>(null);
  const data = task.data;
  const terminal =
    data?.kind === "run" &&
    ["completed", "failed", "stopped"].includes(data.status);
  const insightsRun = data?.kind === "run" && data.mode === "insights";

  // a batch run this page watched finish: say what comes next on the storyline (§3.1)
  const nextStep = useNextStep();
  const showNext = nextStep.show;
  const watching = useRef(false);
  const runStatus = data?.kind === "run" ? data.status : null;
  const runAgent = data?.agentId ?? null;
  useEffect(() => {
    if (runStatus === "running" || runStatus === "queued" || runStatus === "pending") watching.current = true;
    else if (watching.current && runStatus === "completed") {
      watching.current = false;
      if (runAgent) showNext({ done: t("story.done.evalDone"), after: "evaluate.dev_run", agentId: runAgent });
    } else if (runStatus) watching.current = false;
  }, [runStatus, runAgent, showNext, t]);

  const results = useLoad(
    async () => {
      // an insights run carries its clusters on the run row — no judged records to read
      if (!data || insightsRun)
        return { rows: [] as ResultRow[], note: null as string | null };
      if (!terminal)
        return {
          rows: [] as ResultRow[],
          note: t("v2.taskDetail.waitResults"),
        };
      const res = await api.evaluationRunResults(data.id);
      return {
        rows: rowsFromRun(data, res),
        note: res.available
          ? res.truncated
            ? t("v2.taskDetail.truncated")
            : null
          : t(`v2.taskDetail.reason.${res.reason ?? "unreadable"}`),
      };
    },
    `v3-task-results:run:${id}:${data ? `${data.status}` : "none"}`,
  );
  const rows = useMemo(() => results.data?.rows ?? [], [results.data]);
  const summary = useMemo(() => summarize(rows), [rows]);
  // "build an annotation task from this run" (calibration-run-picker §3): the same key
  // as the criteria page's new-task button; the run's evaluator criteria and their
  // sample counts decide whether it is usable and which criterion the link carries
  const mayCalibrate = can("criteria.manage");
  const calibrationAsk =
    mayCalibrate &&
    data?.kind === "run" &&
    calibrateBlocker(data) === null;
  const calibration = useLoad(
    () =>
      calibrationAsk && data
        ? calibrationApi.runCriteria(data.id)
        : Promise.resolve(null),
    `v3-task-calibration:run:${id}:${calibrationAsk ? "ask" : "no"}`,
  );

  const act = async () => {
    if (!confirm || !data) return;
    setBusy(true);
    setFailure(null);
    try {
      await api.stopEvaluationRun(data.id);
      toast("ok", t(`v2.tasks.done.${confirm}`));
      setConfirm(null);
      task.reload();
    } catch (err) {
      setConfirm(null);
      setFailure(err);
    } finally {
      setBusy(false);
    }
  };

  if (task.loading && !data) return <Skeleton rows={6} />;
  if (task.error && !data) return <Notice s="act"><ApiErrorDetail error={task.failure ?? task.error} /></Notice>;
  if (!data) return null;

  const mayRun = can("eval.run");
  const s = taskSignal(data.status);
  const recheck = async () => {
    setBusy(true);
    setFailure(null);
    try {
      const run = await api.recheckEvaluationRun(data.id);
      // still failing stays in place with the page's other failures (storyline-ux §4)
      if (run.status === "failed") setFailure(t("v2.tasks.recheckStillFailed"));
      else toast("ok", t("v2.tasks.done.recheck"));
      task.reload();
    } catch (err) {
      setFailure(err);
    } finally {
      setBusy(false);
    }
  };
  const calibrationKeys = calibratableCriteria(calibration.data?.criteria);
  const calibrationWhy = calibrateBlocker(
    data,
    calibration.data ? calibrationKeys : undefined,
  );
  const calibrateTo = (criterion: string | null) =>
    data.agentId && navigate(calibrateUrl(data.agentId, data.id, criterion));
  const scorable = summary.total - summary.errors;
  const passRate =
    scorable > 0 ? Math.round((summary.passed / scorable) * 100) : null;

  return (
    <>
      <div className="v3-reveal v3-task" style={{ display: "grid", gap: 16 }}>
        <div>
          <button
            type="button"
            className="v3-btn ghost sm"
            onClick={() => navigate("/v3/tasks")}
          >
            <ArrowLeft size={14} /> {t("v3.tasks.title")}
          </button>
        </div>
        <PageHead
          eyebrow={t("v2.tasks.strategyHistory")}
          title={
            <span
              style={{ display: "inline-flex", alignItems: "center", gap: 14 }}
            >
              <Lamp s={s} live={s === "info"} />
              {data.name}
            </span>
          }
          sub={
            <span>
              <Chip s={s === "off" ? undefined : s}>
                {statusLabel(t, data.status)}
              </Chip>{" "}
              {data.agentName} · {sourceLabel(t, data)}
              {data.description ? ` — ${data.description}` : ""}
              <span style={{ display: "block", marginTop: 6 }}>
                <CopyId id={data.id} kind={data.kind} labelled />
              </span>
            </span>
          }
          end={
            <>
              {data.kind === "run" &&
                (data.status === "running" || data.status === "queued") && (
                  <Btn
                    kind="danger"
                    disabled={!mayRun}
                    disabledReason={permReason(t, "eval.run")}
                    onClick={() => setConfirm("stop")}
                  >
                    <Square size={13} /> {t("v2.tasks.stop")}
                  </Btn>
                )}
              {data.kind === "run" &&
                data.status === "failed" &&
                data.run?.batch_eval_id && (
                  <Btn
                    busy={busy}
                    disabled={!mayRun}
                    disabledReason={permReason(t, "eval.run")}
                    title={t("v2.tasks.recheckHint")}
                    onClick={() => void recheck()}
                  >
                    <RotateCw size={13} /> {t("v2.tasks.recheck")}
                  </Btn>
                )}
              {data.kind === "run" && mayCalibrate && (
                <Btn
                  busy={calibration.loading}
                  disabled={calibrationWhy !== null}
                  disabledReason={
                    calibrationWhy
                      ? t(`v3.tasks.calibrateBlocked.${calibrationWhy}`)
                      : null
                  }
                  onClick={() => calibrateTo(soleCriterion(calibrationKeys))}
                >
                  <Tag size={13} /> {t("v3.tasks.calibrate")}
                </Btn>
              )}
              <Btn disabled={!mayRun} disabledReason={permReason(t, "eval.run")} onClick={() => navigate(copyUrl(data))}>
                <Copy size={13} /> {t("v2.tasks.copy")}
              </Btn>
              <Btn
                kind="ghost"
                onClick={() => task.reload()}
                title={t("v3.tasks.refresh")}
              >
                <RefreshCw size={14} />
              </Btn>
            </>
          }
        />
        <ErrorCallout error={failure} params={{ agent_id: data.agentId ?? undefined }} onDismiss={() => setFailure(null)}
          data-testid="v3-task-error" />
        {nextStep.card}

        {mayCalibrate && calibrationWhy === null && calibrationKeys.length > 1 && (
          <div
            style={{
              display: "flex",
              gap: 8,
              alignItems: "center",
              flexWrap: "wrap",
              color: "var(--v3-text-2)",
            }}
          >
            <span>{t("v3.tasks.calibrateCriteria")}</span>
            {(calibration.data?.criteria ?? [])
              .filter((c) => c.items > 0)
              .map((c) => (
                <Btn
                  key={c.key}
                  size="sm"
                  kind="ghost"
                  title={t("v3.tasks.calibrateCriterion")}
                  onClick={() => calibrateTo(c.key)}
                >
                  <Tag size={12} /> <span className="mono">{c.key}</span>{" "}
                  · {t("v2.dlc.calibration.runItems", { count: c.items })}
                </Btn>
              ))}
          </div>
        )}

        {data.run?.error && (
          <Notice s={data.status === "failed" ? "act" : "wait"}>
            {data.run.error}
          </Notice>
        )}
        {!!data.run?.budget_stops?.length && (
          <Notice s="wait">
            {t("v2.taskDetail.budgetStops", {
              count: data.run.budget_stops.length,
              list: data.run.budget_stops
                .map((b) => `${b.scenario_id} (${b.stop_reason || b.code})`)
                .join(", "),
            })}
          </Notice>
        )}

        {data.kind === "run" && (
          <Panel
            title={t("v3.tasks.progress")}
            signal={s === "off" ? undefined : s}
            end={
              data.run?.queue_position
                ? t("v2.taskDetail.queuePos", { n: data.run.queue_position })
                : undefined
            }
          >
            <Track nodes={runTrack(data, t)} />
          </Panel>
        )}

        {data.kind === "run" && (
          <RunCostCard
            runId={data.id}
            running={data.status === "running" || data.status === "queued" || data.status === "pending"}
          />
        )}

        {!insightsRun && (
          <div className="v3-grid c4">
            <Panel>
              <Stat
                label={t("v2.insights.kpiCount")}
                value={summary.total}
                foot={t("v2.insights.kpiCountSub", {
                  passed: summary.passed,
                  failed: summary.failed,
                  errors: summary.errors,
                })}
              />
            </Panel>
            <Panel signal={scoreSignal(summary.mean)}>
              <Stat
                label={t("v2.insights.kpiMean")}
                value={summary.mean == null ? "—" : summary.mean.toFixed(2)}
                signal={scoreSignal(summary.mean)}
                foot={t("v2.insights.kpiMeanSub")}
              />
            </Panel>
            <Panel signal={summary.failed > 0 ? "act" : undefined}>
              <Stat
                label="Bad Case"
                value={summary.failed}
                signal={summary.failed > 0 ? "act" : undefined}
                foot={t("v2.insights.kpiBadSub")}
              />
            </Panel>
            <Panel>
              <Stat
                label={t("v2.insights.kpiPassRate")}
                value={passRate == null ? "—" : `${passRate}%`}
                foot={t("v2.insights.kpiPassRateSub")}
              />
            </Panel>
          </div>
        )}

        <div className="v3-grid v3-split" style={{ alignItems: "start" }}>
          {insightsRun ? (
            <Panel title={t("v2.taskDetail.insightsTitle")}>
              {hasInsightTrees(data.run?.insights) ? (
                <HostedV2>
                  <InsightClusters insights={data.run?.insights ?? {}} />
                </HostedV2>
              ) : (
                <Notice>
                  {terminal
                    ? t("v2.taskDetail.insightsEmpty")
                    : t("v2.taskDetail.insightsWait")}
                </Notice>
              )}
            </Panel>
          ) : (
            <Panel
              title={t("v2.insights.byEvaluator")}
              end={<span>{t("v2.insights.byEvaluatorSub")}</span>}
            >
              {summary.byEvaluator.length === 0 ? (
                <p className="v3-task-muted">
                  {results.data?.note ?? t("v2.insights.empty")}
                </p>
              ) : (
                <div style={{ display: "grid", gap: 10 }}>
                  {summary.byEvaluator.map((e) => (
                    <div key={e.evaluatorId} className="v3-task-ev">
                      <span className="nm" title={e.evaluatorId}>
                        {evaluatorLabel(t, e.evaluatorId)}
                      </span>
                      <span className="bar">
                        <i
                          data-s={scoreSignal(e.mean) ?? "off"}
                          style={{
                            width: `${Math.round((e.mean ?? 0) * 100)}%`,
                          }}
                        />
                      </span>
                      <ScoreChip value={e.mean} />
                      <span className="n">
                        {t("v2.insights.evalCounts", {
                          count: e.count,
                          failed: e.failed,
                        })}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </Panel>
          )}
          <Panel title={t("v2.taskDetail.overview")}>
            <dl className="v3-kv">
              {data.logSource ? (
                <>
                  <dt>{t("v2.tasks.cw.target")}</dt>
                  <dd>{t("v2.tasks.cw.targetCw")}</dd>
                  <dt>{t("v2.tasks.cw.service")}</dt>
                  <dd className="mono">{data.logSource.service_name}</dd>
                  <dt>{t("v2.tasks.cw.groupsLabel")}</dt>
                  <dd>
                    <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      {data.logSource.log_group_names.map((g) => (
                        <Chip key={g}>{g}</Chip>
                      ))}
                    </span>
                  </dd>
                </>
              ) : (
                <>
                  <dt>{t("v2.tasks.colAgent")}</dt>
                  <dd>
                    {data.agentId ? (
                      <a
                        href={`/v3/agents?id=${data.agentId}`}
                        onClick={(e) => {
                          e.preventDefault();
                          navigate(`/v3/agents?id=${data.agentId}`);
                        }}
                      >
                        {data.agentName}
                      </a>
                    ) : (
                      data.agentName
                    )}
                  </dd>
                </>
              )}
              <dt>{t("v2.tasks.colSource")}</dt>
              <dd>{sourceLabel(t, data)}</dd>
              {insightsRun && (
                <>
                  <dt>{t("v2.tasks.mode")}</dt>
                  <dd>{t("v2.tasks.modeInsights")}</dd>
                </>
              )}
              <dt>
                {insightsRun
                  ? t("evalPage.newRun.insightTypes")
                  : t("v2.tasks.colEvaluators")}
              </dt>
              <dd>
                <span style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {data.evaluators.map((e) => (
                    <Chip key={e}>{taskItemLabel(t, data, e)}</Chip>
                  ))}
                </span>
              </dd>
              <dt>{t("v2.taskDetail.sessions")}</dt>
              <dd className="mono">{data.run?.session_ids.length ?? 0}</dd>
              <dt>{t("v2.tasks.colCreated")}</dt>
              <dd>{fmtTime(data.createdAt)}</dd>
              <dt>{t("v2.tasks.colUpdated")}</dt>
              <dd>{fmtTime(data.updatedAt)}</dd>
            </dl>
          </Panel>
        </div>

        {!insightsRun && (
          <Panel
            title={t("v2.taskDetail.results")}
            flush
          >
            {results.data?.note && (
              <div style={{ padding: "0 20px 12px" }}>
                <Notice s="wait">{results.data.note}</Notice>
              </div>
            )}
            <Results
              rows={rows}
              loading={results.loading}
              error={results.error}
              onRetry={results.reload}
              range={data.kind === "online" ? range : "7d"}
              exportName={`task-${data.id}`}
            />
          </Panel>
        )}

        {confirm && (
          <Confirm
            title={t(`v2.tasks.confirm.${confirm}Title`)}
            confirmLabel={t(`v2.tasks.confirm.${confirm}Ok`)}
            cancelLabel={t("v3.common.cancel")}
            danger={confirm === "stop"}
            busy={busy}
            onCancel={() => setConfirm(null)}
            onConfirm={() => void act()}
          >
            {t(`v2.tasks.confirm.${confirm}Body`, { name: data.name })}
          </Confirm>
        )}
      </div>
      {/* outside the reveal: its V2 modal is position: fixed, which a revealed
        (transformed) block would trap. Keyed on status: the inputs'
        eligibility flips when the run completes. */}
      {data.kind === "run" && data.run && (
        <div style={{ marginTop: 16 }}>
          <HostedV2>
            <RunRecommendations key={data.status} run={data.run} />
          </HostedV2>
        </div>
      )}
    </>
  );
}

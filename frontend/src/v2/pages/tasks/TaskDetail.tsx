import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, errorMessage, type V2Range } from "../../../lib/api";
import { evaluatorLabel } from "../../../lib/evaluators";
import { fmtTime, RANGES, rangeLabel } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { EvaluatorBreakdown, ResultsTable, SummaryKpis } from "../../ResultsView";
import { rowsFromOnline, rowsFromRun, summarize } from "../../results";
import { sourceLabel, STATUS_TONE, statusLabel, taskFromOnline, taskFromRun, type TaskKind, type V2Task } from "../../tasks";
import { Alert, Button, Card, Confirm, Descriptions, FilterSelect, FlowHeader, Spin, Tag } from "../../ui";

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
  return { ...task, reload: () => setTick((n) => n + 1) };
}

export function TaskDetail({ kind, id }: { kind: TaskKind; id: string }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { can } = useAuth();
  const task = useTask(kind, id);
  const [range, setRange] = useState<V2Range>("24h");
  const [confirm, setConfirm] = useState<"stop" | "pause" | "resume" | null>(null);
  const [busy, setBusy] = useState(false);
  const data = task.data;
  const terminal = data?.kind === "run" && ["completed", "failed", "stopped"].includes(data.status);

  const results = useLoad(
    async () => {
      if (!data) return { rows: [], note: null as string | null };
      if (data.kind === "run") {
        if (!terminal) return { rows: [], note: t("v2.taskDetail.waitResults") };
        const res = await api.evaluationRunResults(data.id);
        return {
          rows: rowsFromRun(data, res),
          note: res.available ? (res.truncated ? t("v2.taskDetail.truncated") : null) : t(`v2.taskDetail.reason.${res.reason ?? "unreadable"}`),
        };
      }
      const res = await api.v2OnlineResults(data.id, range);
      return { rows: rowsFromOnline(data, res.recent), note: res.errors.count ? t("v2.taskDetail.onlineErrors", { count: res.errors.count }) : null };
    },
    `task-results:${kind}:${id}:${data ? `${data.status}` : "none"}:${range}`,
  );
  const rows = useMemo(() => results.data?.rows ?? [], [results.data]);
  const summary = useMemo(() => summarize(rows), [rows]);

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
  const actions = (
    <>
      {data.kind === "run" && (data.status === "running" || data.status === "queued") && (
        <Button kind="danger" disabled={!mayRun} onClick={() => setConfirm("stop")}>
          {t("v2.tasks.stop")}
        </Button>
      )}
      {data.kind === "online" && data.status === "running" && <Button onClick={() => setConfirm("pause")}>{t("v2.tasks.pause")}</Button>}
      {data.kind === "online" && data.status === "paused" && (
        <Button disabled={!mayRun} onClick={() => setConfirm("resume")}>
          {t("v2.tasks.resume")}
        </Button>
      )}
      <Button disabled={!mayRun} onClick={() => setParams({ view: "new", from: `${data.kind}:${data.id}` })}>
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
            <Tag tone={STATUS_TONE[data.status]}>{statusLabel(t, data.status)}</Tag>
          </span>
        }
        onBack={() => setParams({})}
        end={actions}
      />
      {data.run?.error && <Alert tone={data.status === "failed" ? "error" : "warn"}>{data.run.error}</Alert>}
      {data.online?.failure_reason && <Alert tone="error">{data.online.failure_reason}</Alert>}
      <Card title={t("v2.taskDetail.overview")}>
        <Descriptions
          items={[
            { label: t("v2.tasks.colName"), value: data.name },
            { label: "ID", value: <span className="mono">{data.id}</span> },
            { label: t("v2.tasks.colAgent"), value: data.agentName },
            { label: t("v2.tasks.colSource"), value: sourceLabel(t, data) },
            {
              label: t("v2.tasks.colStrategy"),
              value: data.kind === "online" ? t("v2.tasks.strategyContinuous") : t("v2.tasks.strategyHistory"),
            },
            {
              label: t("v2.tasks.colEvaluators"),
              value: (
                <div className="v2-tags">
                  {data.evaluators.map((e) => (
                    <Tag key={e} tone="outline">
                      {evaluatorLabel(t, e)}
                    </Tag>
                  ))}
                </div>
              ),
            },
            { label: t("v2.tasks.colCreated"), value: fmtTime(data.createdAt) },
            { label: t("v2.tasks.colUpdated"), value: fmtTime(data.updatedAt) },
            ...(data.kind === "run"
              ? [
                  { label: t("v2.taskDetail.sessions"), value: data.run?.session_ids.length ?? 0 },
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

      <SummaryKpis summary={summary} />
      <EvaluatorBreakdown summary={summary} />

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
          exportName={`task-${data.id}`}
        />
      </Card>

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

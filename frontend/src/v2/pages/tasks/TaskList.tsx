import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, errorMessage } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
import { Score } from "../../ResultsView";
import { runMean } from "../../scorecard";
import {
  evaluatorSummary,
  loadRunTasks,
  sourceLabel,
  STATUS_TONE,
  statusLabel,
  TASK_SOURCES,
  taskItemLabel,
  type TaskMode,
  type TaskStatus,
  type V2Task,
} from "../../tasks";
import {
  type AttentionItem,
  AttentionPanel,
  Button,
  Card,
  type Column,
  Confirm,
  FilterSelect,
  Lamp,
  LinkButton,
  Pager,
  SearchInput,
  Table,
  Tag,
} from "../../ui";
import { failedTasks, TASK_ATTENTION_CAP } from "./attention";
import { backState } from "./back";
import { versionHint, versionShort } from "./scorecardText";

type Pending = { task: V2Task; action: "stop" | "delete" };

/** Run sources: live traffic is the 持续评估 tab's (online configs), never a run's. */
const RUN_SOURCES = TASK_SOURCES.filter((s) => s !== "live");

/** 一次性评估 (`?tab=runs`): every batch evaluation run. The page header and the tabs
 *  belong to `V2Tasks`; this renders the attention panel and the table under them. */
export function TaskList() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const location = useLocation();
  const toast = useV2Toast();
  const { can } = useAuth();
  const { data, loading, error, reload } = useLoad(loadRunTasks, "run-tasks");
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("");
  const [mode, setMode] = useState("");
  const [agent, setAgent] = useState("");
  const [q, setQ] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);

  const tasks = useMemo(() => data ?? [], [data]);
  const agents = useMemo(() => [...new Set(tasks.map((task) => task.agentName))].sort(), [tasks]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return tasks.filter((task) => {
      if (status && task.status !== status) return false;
      if (source && task.source !== source) return false;
      if (mode && task.mode !== mode) return false;
      if (agent && task.agentName !== agent) return false;
      return !needle || `${task.name} ${task.id} ${task.description}`.toLowerCase().includes(needle);
    });
  }, [tasks, status, source, mode, agent, q]);
  const paged = usePaged(rows, 12);
  const mayRun = can("eval.run");

  const modeLabel = (m: TaskMode) => t(m === "insights" ? "v2.tasks.modeInsightsShort" : "v2.tasks.modeEvaluators");
  // the detail / wizard's Back returns to this tab (`back.ts`)
  const back = { state: backState(location.search) };
  const open = (task: V2Task) => setParams({ view: "detail", kind: "run", id: task.id }, back);
  const copy = (task: V2Task) => setParams({ view: "new", from: `run:${task.id}` }, back);

  const attention: AttentionItem[] = failedTasks(tasks).map((entry) => ({
    key: entry.key,
    signal: entry.signal,
    title: entry.task.name,
    hint: entry.detail || statusLabel(t, entry.task.status),
    meta: `${entry.task.agentName} · ${fmtTime(entry.task.createdAt)}`,
    onOpen: () => open(entry.task),
    openLabel: t("v2.common.view"),
    testId: `v2-tasks-att-${entry.task.kind}-${entry.task.id}`,
  }));

  const act = async () => {
    if (!pending) return;
    const { task, action } = pending;
    setBusy(true);
    try {
      if (action === "stop") await api.stopEvaluationRun(task.id);
      else await api.deleteEvaluationRun(task.id);
      toast("success", t(`v2.tasks.done.${action}`));
      setPending(null);
      reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<V2Task>[] = [
    {
      key: "name",
      title: t("v2.tasks.colNameId"),
      render: (task) => (
        <>
          <LinkButton onClick={() => open(task)}>
            <span className="v2-fluid">
              <span className="ellipsis" style={{ maxWidth: 240 }} title={task.name}>{task.name}</span>
            </span>
          </LinkButton>
          <span className="sub mono">ID: {task.id}</span>
        </>
      ),
    },
    {
      // the mode rides under the evaluators: with 平均分 the table stays within 8 columns
      key: "desc",
      title: t("v2.tasks.colEvaluators"),
      render: (task) => {
        const label = (id: string) => taskItemLabel(t, task, id);
        return (
          <>
            <span className="v2-fluid">
              <span className="ellipsis" style={{ maxWidth: 160 }} title={task.evaluators.map(label).join("、")}>
                {evaluatorSummary(t, task.evaluators, label)}
              </span>
            </span>
            <span className="sub" data-testid={`v2-task-mode-${task.id}`}>
              <Tag tone={task.mode === "insights" ? "blue" : "outline"}>{modeLabel(task.mode)}</Tag>
            </span>
          </>
        );
      },
    },
    {
      key: "agent",
      title: t("v2.tasks.colAgent"),
      render: (task) => (
        <>
          <span className="v2-fluid">
            <span className={task.logSource ? "ellipsis mono" : "ellipsis"} style={{ maxWidth: 160 }} title={task.agentName}>
              {task.agentName}
            </span>
          </span>
          {task.logSource && <span className="sub">{t("v2.tasks.cw.short")}</span>}
          {task.run?.snapshot && (
            <span className="sub" title={versionHint(t, task.run.snapshot)} data-testid={`v2-task-version-${task.id}`}>
              {t("v2.scorecard.versionLine", { version: versionShort(t, task.run.snapshot) })}
            </span>
          )}
        </>
      ),
    },
    {
      key: "source",
      title: t("v2.tasks.colSource"),
      render: (task) => (
        <span className="v2-fluid">
          <span className="ellipsis" style={{ maxWidth: 180 }} title={sourceLabel(t, task)}>{sourceLabel(t, task)}</span>
        </span>
      ),
    },
    {
      // the run's own mean of its normalized evaluator means; none for insights
      key: "avg",
      title: t("v2.scorecard.col.avg"),
      className: "nowrap",
      render: (task) => <Score value={task.mode === "evaluators" && task.run ? runMean(task.run) : null} />,
    },
    {
      key: "status",
      title: t("v2.tasks.colStatus"),
      render: (task) => <Tag tone={STATUS_TONE[task.status]}>{statusLabel(t, task.status)}</Tag>,
    },
    {
      key: "time",
      title: t("v2.tasks.colCreatedUpdated"),
      className: "nowrap",
      render: (task) => (
        <>
          {fmtTime(task.createdAt)}
          <span className="sub" title={t("v2.tasks.colUpdated")}>{fmtTime(task.updatedAt)}</span>
          <span className="sub" title={t("v2.common.creator")}>{task.createdByName || "—"}</span>
        </>
      ),
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right sticky",
      render: (task) => {
        const active = task.status === "running" || task.status === "queued";
        return (
          <div className="v2-actions">
            <LinkButton onClick={() => open(task)}>{t("v2.common.view")}</LinkButton>
            {active && (
              <LinkButton disabled={!mayRun} onClick={() => setPending({ task, action: "stop" })}>
                {t("v2.tasks.stop")}
              </LinkButton>
            )}
            <LinkButton disabled={!mayRun} onClick={() => copy(task)}>
              {t("v2.tasks.copy")}
            </LinkButton>
            <LinkButton danger disabled={!mayRun || active} onClick={() => setPending({ task, action: "delete" })}>
              {t("v2.common.delete")}
            </LinkButton>
          </div>
        );
      },
    },
  ];

  const statuses: TaskStatus[] = ["queued", "running", "completed", "failed", "stopped"];

  return (
    <>
      {data && (
        <AttentionPanel
          title={
            <>
              {t("v2.tasks.attention.title")}
              <span className="sub">{t("v2.tasks.attention.sub")}</span>
            </>
          }
          items={attention}
          cap={TASK_ATTENTION_CAP}
          dismissScope="eval.tasks"
          testId="v2-tasks-attention"
          empty={
            <>
              <Lamp signal={tasks.length ? "ok" : "off"} />
              {tasks.length ? t("v2.tasks.attention.empty") : t("v2.tasks.empty")}
            </>
          }
        />
      )}
      <Card>
        <div className="v2-toolbar">
          <Button onClick={reload}>{t("v2.common.refresh")}</Button>
          <Button kind="primary" disabled={!mayRun} title={mayRun ? undefined : t("v2.tasks.noPermission")} onClick={() => setParams({ view: "new" }, back)} testId="v2-task-new">
            <Plus size={14} aria-hidden="true" />
            {t("v2.tasks.new")}
          </Button>
          <FilterSelect
            label={t("v2.tasks.colStatus")}
            value={status}
            allLabel={t("v2.common.all")}
            onChange={setStatus}
            options={statuses.map((s) => ({ value: s, label: statusLabel(t, s) }))}
          />
          <FilterSelect
            label={t("v2.tasks.colSource")}
            value={source}
            allLabel={t("v2.common.all")}
            onChange={setSource}
            options={RUN_SOURCES.map((s) => ({
              value: s,
              label: t(`v2.taskSource.${s}Short`),
            }))}
          />
          <FilterSelect
            label={t("v2.tasks.mode")}
            value={mode}
            allLabel={t("v2.common.all")}
            onChange={setMode}
            options={(["evaluators", "insights"] as const).map((m) => ({ value: m, label: modeLabel(m) }))}
          />
          <FilterSelect
            label={t("v2.tasks.colAgent")}
            value={agent}
            allLabel={t("v2.common.all")}
            onChange={setAgent}
            options={agents.map((a) => ({ value: a, label: a }))}
          />
          <div className="end">
            <SearchInput value={q} onChange={setQ} placeholder={t("v2.tasks.search")} />
            <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
          </div>
        </div>
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(task) => task.id}
          loading={loading}
          error={error}
          onRetry={reload}
          empty={t("v2.tasks.empty")}
          testId="v2-tasks-table"
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
      <Confirm
        open={pending !== null}
        title={pending ? t(`v2.tasks.confirm.${pending.action}Title`) : ""}
        body={pending ? t(`v2.tasks.confirm.${pending.action}Body`, { name: pending.task.name }) : null}
        confirmLabel={pending ? t(`v2.tasks.confirm.${pending.action}Ok`) : ""}
        danger={pending?.action === "delete" || pending?.action === "stop"}
        busy={busy}
        onConfirm={() => void act()}
        onClose={() => setPending(null)}
      />
    </>
  );
}

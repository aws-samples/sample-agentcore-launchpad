import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, errorMessage } from "../../../lib/api";
import { evaluatorLabel } from "../../../lib/evaluators";
import { fmtTime } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
import {
  evaluatorSummary,
  loadTasks,
  sourceLabel,
  STATUS_TONE,
  statusLabel,
  type TaskStatus,
  type V2Task,
} from "../../tasks";
import {
  Button,
  Card,
  type Column,
  Confirm,
  FilterSelect,
  LinkButton,
  PageHeader,
  Pager,
  SearchInput,
  Table,
  Tag,
} from "../../ui";

type Pending = { task: V2Task; action: "stop" | "pause" | "resume" | "delete" };

export function TaskList() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { can } = useAuth();
  const { data, loading, error, reload } = useLoad(loadTasks, "tasks");
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("");
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
      if (agent && task.agentName !== agent) return false;
      return !needle || `${task.name} ${task.id} ${task.description}`.toLowerCase().includes(needle);
    });
  }, [tasks, status, source, agent, q]);
  const paged = usePaged(rows, 12);
  const label = (id: string) => evaluatorLabel(t, id);
  const mayRun = can("eval.run");

  const open = (task: V2Task) => setParams({ view: "detail", kind: task.kind, id: task.id });
  const copy = (task: V2Task) => setParams({ view: "new", from: `${task.kind}:${task.id}` });

  const act = async () => {
    if (!pending) return;
    const { task, action } = pending;
    setBusy(true);
    try {
      if (task.kind === "run") {
        if (action === "stop") await api.stopEvaluationRun(task.id);
        else if (action === "delete") await api.deleteEvaluationRun(task.id);
      } else if (action === "delete") {
        await api.v2DeleteOnlineConfig(task.id);
      } else if (action === "pause" || action === "resume") {
        await api.v2OnlineAction(task.id, action);
      }
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
            <span className="ellipsis" style={{ maxWidth: 320 }} title={task.name}>{task.name}</span>
          </LinkButton>
          <span className="sub mono">ID: {task.id}</span>
        </>
      ),
    },
    {
      key: "desc",
      title: t("v2.tasks.colEvaluators"),
      render: (task) => (
        <span className="ellipsis" style={{ maxWidth: 180 }} title={task.evaluators.map(label).join("、")}>
          {evaluatorSummary(t, task.evaluators, label)}
        </span>
      ),
    },
    {
      key: "agent",
      title: t("v2.tasks.colAgent"),
      render: (task) => <span className="ellipsis" title={task.agentName}>{task.agentName}</span>,
    },
    {
      key: "source",
      title: t("v2.tasks.colSource"),
      render: (task) => <span className="ellipsis" title={sourceLabel(t, task)}>{sourceLabel(t, task)}</span>,
    },
    {
      key: "strategy",
      title: t("v2.tasks.colStrategy"),
      className: "nowrap",
      render: (task) => (task.kind === "online" ? t("v2.tasks.strategyContinuous") : t("v2.tasks.strategyHistory")),
    },
    {
      key: "status",
      title: t("v2.tasks.colStatus"),
      render: (task) => <Tag tone={STATUS_TONE[task.status]}>{statusLabel(t, task.status)}</Tag>,
    },
    { key: "created", title: t("v2.tasks.colCreated"), className: "nowrap", render: (task) => fmtTime(task.createdAt) },
    { key: "updated", title: t("v2.tasks.colUpdated"), className: "nowrap", render: (task) => fmtTime(task.updatedAt) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (task) => {
        const active = task.status === "running" || task.status === "queued";
        return (
          <div className="v2-actions">
            <LinkButton onClick={() => open(task)}>{t("v2.common.view")}</LinkButton>
            {task.kind === "run" && active && (
              <LinkButton disabled={!mayRun} onClick={() => setPending({ task, action: "stop" })}>
                {t("v2.tasks.stop")}
              </LinkButton>
            )}
            {task.kind === "online" && task.status === "running" && (
              <LinkButton onClick={() => setPending({ task, action: "pause" })}>{t("v2.tasks.pause")}</LinkButton>
            )}
            {task.kind === "online" && task.status === "paused" && (
              <LinkButton disabled={!mayRun} onClick={() => setPending({ task, action: "resume" })}>
                {t("v2.tasks.resume")}
              </LinkButton>
            )}
            <LinkButton disabled={!mayRun} onClick={() => copy(task)}>
              {t("v2.tasks.copy")}
            </LinkButton>
            <LinkButton danger disabled={(task.kind === "run" && (!mayRun || active))} onClick={() => setPending({ task, action: "delete" })}>
              {t("v2.common.delete")}
            </LinkButton>
          </div>
        );
      },
    },
  ];

  const statuses: TaskStatus[] = ["queued", "running", "completed", "failed", "stopped", "paused"];

  return (
    <>
      <PageHeader title={t("v2.tasks.title")} desc={t("v2.tasks.desc")} />
      <Card>
        <div className="v2-toolbar">
          <Button onClick={reload}>{t("v2.common.refresh")}</Button>
          <Button kind="primary" disabled={!mayRun} title={mayRun ? undefined : t("v2.tasks.noPermission")} onClick={() => setParams({ view: "new" })} testId="v2-task-new">
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
            options={(["window", "sessions", "dataset", "cloud", "live"] as const).map((s) => ({
              value: s,
              label: t(`v2.taskSource.${s}Short`),
            }))}
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
          rowKey={(task) => `${task.kind}:${task.id}`}
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

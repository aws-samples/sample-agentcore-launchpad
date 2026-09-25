import { ArrowRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../../lib/api";
import { evaluatorLabel } from "../../lib/evaluators";
import { fmtTime } from "../format";
import { useLoad } from "../hooks";
import { sortTasks, STATUS_TONE, statusLabel, taskFromOnline, taskFromRun, type V2Task } from "../tasks";
import { Button, Card, type Column, Kpi, LinkButton, PageHeader, Table, Tag } from "../ui";

async function loadHome() {
  // Each tile degrades on its own: one unreachable service must not blank the page.
  const [agents, runs, online, datasets, dashboard] = await Promise.allSettled([
    api.listAgents(),
    api.listEvaluationRuns({ limit: 50 }),
    api.v2OnlineConfigs(),
    api.v2Datasets(),
    api.obsDashboard("24h"),
  ]);
  const value = <T,>(r: PromiseSettledResult<T>): T | null => (r.status === "fulfilled" ? r.value : null);
  const runRows = value(runs)?.runs ?? [];
  const onlineRows = (value(online)?.configs ?? []).filter((c) => c.owner === "agent");
  const tasks = sortTasks([...runRows.map(taskFromRun), ...onlineRows.map(taskFromOnline)]);
  return {
    activeAgents: value(agents)?.agents.filter((a) => a.status === "active").length ?? null,
    tasks,
    datasets: value(datasets)?.datasets.length ?? null,
    traces: value(dashboard)?.tiles.traces.total ?? null,
    errorRate: value(dashboard)?.tiles.error_rate ?? null,
  };
}

export function V2Home() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { data, loading, error, reload } = useLoad(loadHome, "home");

  const running = data?.tasks.filter((task) => task.status === "running").length ?? 0;
  const steps = [
    { to: "/v2/eval/data?tab=datasets", title: t("v2.home.step1"), desc: t("v2.home.step1Desc") },
    { to: "/v2/eval/evaluators", title: t("v2.home.step2"), desc: t("v2.home.step2Desc") },
    { to: "/v2/eval/tasks?view=new", title: t("v2.home.step3"), desc: t("v2.home.step3Desc") },
    { to: "/v2/eval/insights", title: t("v2.home.step4"), desc: t("v2.home.step4Desc") },
  ];

  const columns: Column<V2Task>[] = [
    {
      key: "name",
      title: t("v2.tasks.colName"),
      render: (task) => (
        <LinkButton onClick={() => navigate(`/v2/eval/tasks?view=detail&kind=${task.kind}&id=${task.id}`)}>
          {task.name}
        </LinkButton>
      ),
    },
    { key: "agent", title: t("v2.tasks.colAgent"), render: (task) => task.agentName },
    {
      key: "evaluators",
      title: t("v2.tasks.colEvaluators"),
      render: (task) =>
        task.evaluators.length ? evaluatorLabel(t, task.evaluators[0]) + (task.evaluators.length > 1 ? ` +${task.evaluators.length - 1}` : "") : "—",
    },
    {
      key: "status",
      title: t("v2.tasks.colStatus"),
      render: (task) => <Tag tone={STATUS_TONE[task.status]}>{statusLabel(t, task.status)}</Tag>,
    },
    { key: "created", title: t("v2.tasks.colCreated"), className: "nowrap", render: (task) => fmtTime(task.createdAt) },
  ];

  return (
    <>
      <PageHeader title={t("v2.home.title")} desc={t("v2.home.desc")} />
      <div className="v2-kpis">
        <Kpi label={t("v2.home.kpiAgents")} value={data?.activeAgents ?? "—"} sub={t("v2.home.kpiAgentsSub")} testId="v2-kpi-agents" />
        <Kpi
          label={t("v2.home.kpiTasks")}
          value={data ? data.tasks.length : "—"}
          sub={t("v2.home.kpiTasksSub", { count: running })}
        />
        <Kpi label={t("v2.home.kpiDatasets")} value={data?.datasets ?? "—"} sub={t("v2.home.kpiDatasetsSub")} />
        <Kpi
          label={t("v2.home.kpiTraces")}
          value={data?.traces ?? "—"}
          sub={
            data?.errorRate == null
              ? t("v2.home.kpiTracesSub")
              : t("v2.home.kpiTracesErr", { rate: (data.errorRate * 100).toFixed(1) })
          }
        />
      </div>
      <Card title={t("v2.home.flowTitle")} sub={t("v2.home.flowSub")}>
        <div className="v2-flow">
          {steps.map((step, i) => (
            <Link key={step.to} to={step.to} className="v2-flow-step">
              <span className="n">{i + 1}</span>
              <span className="t">{step.title}</span>
              <span className="d">{step.desc}</span>
            </Link>
          ))}
        </div>
      </Card>
      <Card
        title={t("v2.home.recentTasks")}
        flush
        end={
          <Button size="sm" onClick={() => navigate("/v2/eval/tasks")}>
            {t("v2.home.allTasks")}
            <ArrowRight size={13} aria-hidden="true" />
          </Button>
        }
      >
        <div style={{ padding: "0 24px 16px" }}>
          <Table
            columns={columns}
            rows={(data?.tasks ?? []).slice(0, 5)}
            rowKey={(task) => `${task.kind}:${task.id}`}
            loading={loading}
            error={error}
            onRetry={reload}
            empty={t("v2.tasks.empty")}
          />
        </div>
      </Card>
    </>
  );
}

export function V2NotFound() {
  const { t } = useTranslation();
  return (
    <Card>
      <div className="v2-table-empty">
        <h2 style={{ fontSize: 18, marginBottom: 8, color: "var(--v2-ink)" }}>{t("v2.notFound.title")}</h2>
        <p style={{ marginBottom: 16 }}>{t("v2.notFound.desc")}</p>
        <Link to="/v2" className="v2-btn primary">
          {t("v2.notFound.home")}
        </Link>
      </div>
    </Card>
  );
}

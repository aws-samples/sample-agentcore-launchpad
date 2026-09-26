import { ArrowRight, Sparkles } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../../lib/api";
import { fmtTime } from "../format";
import { useLoad } from "../hooks";
import { sortTasks, STATUS_TONE, statusLabel, taskFromOnline, taskFromRun, type V2Task } from "../tasks";
import { Button, Card, type Column, Kpi, LinkButton, PageHeader, Table, Tag } from "../ui";
import { AnnouncementBoard } from "./home/AnnouncementBoard";
import "./home/home.css";
import { RecentAgents } from "./home/RecentAgents";
import { ServiceHealth } from "./home/ServiceHealth";

async function loadTasks(): Promise<V2Task[]> {
  // one unreachable service must not blank the list
  const [runs, online] = await Promise.allSettled([api.listEvaluationRuns({ limit: 50 }), api.v2OnlineConfigs()]);
  const runRows = runs.status === "fulfilled" ? runs.value.runs : [];
  const onlineRows = online.status === "fulfilled" ? online.value.configs.filter((c) => c.owner === "agent") : [];
  return sortTasks([...runRows.map(taskFromRun), ...onlineRows.map(taskFromOnline)]);
}

/** Ticks every `ms` — put it in a `useLoad` key to poll. */
function useTick(ms: number): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setTick((n) => n + 1), ms);
    return () => clearInterval(timer);
  }, [ms]);
  return tick;
}

// the lifecycle strip: create → debug → observe → evaluate → optimise → govern
const LIFECYCLE = [
  { key: "create", to: "/v2/agents?view=new" },
  { key: "chat", to: "/v2/chat" },
  { key: "observe", to: "/v2/observability" },
  { key: "evaluate", to: "/v2/eval/tasks?view=new" },
  { key: "optimize", to: "/v2/eval/experiments" },
  { key: "govern", to: "/v2/governance" },
] as const;

/**
 * 工作台 — the V2 console's landing page: platform KPIs (the classic Overview's
 * tiles), the create → deploy → invoke → observe → evaluate lifecycle, recent
 * deployments and evaluation tasks, and a right rail with the announcement board
 * and per-service health. Agents poll every 10 s, the overview every 30 s.
 */
export function V2Home() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const fast = useTick(10_000);
  const slow = useTick(30_000);
  const agents = useLoad(() => api.listAgents(), `home-agents:${fast}`);
  const overview = useLoad(() => api.getOverview(), `home-overview:${slow}`);
  const quality = useLoad(() => api.overviewOnlineQuality(), `home-quality:${slow}`);
  const tasks = useLoad(loadTasks, "home-tasks");

  // useLoad keeps the last answer while a poll is in flight or failed
  const info = overview.data;
  const qual = quality.data;
  const list = agents.data?.agents ?? null;
  const active = list ? list.filter((a) => a.status === "active").length : null;
  const inFlight = list ? list.filter((a) => a.status === "deploying").length : 0;
  const assets = info?.registry_assets;
  const running = tasks.data?.filter((task) => task.status === "running").length ?? 0;

  const taskColumns: Column<V2Task>[] = [
    {
      key: "name",
      title: t("v2.tasks.colName"),
      // the agent rides on the sub-line and evaluators stay on the task page, so the table fits the main column
      render: (task) => (
        <>
          <LinkButton onClick={() => navigate(`/v2/eval/tasks?view=detail&kind=${task.kind}&id=${encodeURIComponent(task.id)}`)}>
            <span className="ellipsis" style={{ maxWidth: 320 }} title={task.name}>
              {task.name}
            </span>
          </LinkButton>
          <span className="sub">{task.agentName}</span>
        </>
      ),
    },
    {
      key: "status",
      title: t("v2.tasks.colStatus"),
      render: (task) => <Tag tone={STATUS_TONE[task.status]}>{statusLabel(t, task.status)}</Tag>,
    },
    { key: "created", title: t("v2.tasks.colCreated"), className: "nowrap", render: (task) => fmtTime(task.createdAt) },
  ];

  const qualityTone = qual?.mean == null ? undefined : qual.mean >= 0.8 ? "good" : qual.mean < 0.5 ? "bad" : undefined;

  return (
    <>
      <PageHeader title={t("v2.home.title")} desc={t("v2.home.desc")} />
      <div className="v2-kpis">
        <Kpi
          label={t("v2.home.kpi.agents")}
          value={active ?? "—"}
          sub={inFlight ? t("v2.home.kpi.agentsInFlight", { count: inFlight }) : t("v2.home.kpi.agentsSub")}
          testId="v2-kpi-agents"
        />
        <Kpi label={t("v2.home.kpi.sessions")} value={info ? info.active_sessions : "—"} sub={t("v2.home.kpi.last24h")} testId="v2-kpi-sessions" />
        <Kpi
          label={t("v2.home.kpi.assets")}
          value={assets ? assets.total : "—"}
          sub={assets ? t("overview.tiles.breakdown", { agents: assets.agents, tools: assets.tools, skills: assets.skills }) : undefined}
          testId="v2-kpi-assets"
        />
        <Kpi
          label={t("v2.home.kpi.passRate")}
          value={info?.eval_pass_rate != null ? `${Math.round(info.eval_pass_rate * 100)}%` : "—"}
          sub={info && info.eval_runs > 0 ? t("overview.tiles.runCount", { count: info.eval_runs }) : t("overview.tiles.noRuns")}
          testId="v2-kpi-pass"
        />
        <Kpi
          label={t("v2.home.kpi.quality")}
          value={qual?.mean != null ? `${Math.round(qual.mean * 100)}%` : "—"}
          tone={qualityTone}
          sub={
            qual == null
              ? t("v2.home.kpi.last24h")
              : qual.configs === 0 && qual.scores === 0
                ? t("overview.tiles.onlineQualityNone")
                : qual.scores === 0
                  ? t("overview.tiles.onlineQualityPending")
                  : t("overview.tiles.onlineQualityFoot", { sessions: qual.sessions, agents: qual.agents })
          }
          testId="v2-kpi-quality"
        />
      </div>

      <div className="v2-home-grid">
        <div className="v2-home-main">
          <Card
            title={t("v2.home.flowTitle")}
            sub={t("v2.home.flowSub")}
            end={
              <Button size="sm" kind="soft" onClick={() => navigate("/v2/assistant")}>
                <Sparkles size={13} aria-hidden="true" />
                {t("v2.home.assistant")}
              </Button>
            }
          >
            <div className="v2-home-flow">
              {LIFECYCLE.map((step, i) => (
                <Link key={step.key} to={step.to} className="v2-flow-step" data-testid={`v2-home-step-${step.key}`}>
                  <span className="n">{i + 1}</span>
                  <span className="t">{t(`v2.home.step.${step.key}`)}</span>
                  <span className="d">{t(`v2.home.step.${step.key}Desc`)}</span>
                </Link>
              ))}
            </div>
          </Card>
          <RecentAgents agents={list} loading={agents.data === null && !agents.error} error={agents.error} onRetry={agents.reload} />
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
                columns={taskColumns}
                rows={(tasks.data ?? []).slice(0, 5)}
                rowKey={(task) => `${task.kind}:${task.id}`}
                loading={tasks.loading}
                error={tasks.error}
                onRetry={tasks.reload}
                empty={t("v2.tasks.empty")}
              />
            </div>
            {running > 0 && <p className="v2-muted v2-home-running">{t("v2.home.kpiTasksSub", { count: running })}</p>}
          </Card>
        </div>
        <aside className="v2-home-rail">
          <AnnouncementBoard />
          <ServiceHealth info={info} error={overview.error} onRetry={overview.reload} activeAgents={active} />
        </aside>
      </div>
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

import type { CSSProperties } from "react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { Chip, DataTable, MethodChip, Panel, StatTile, ViewHead } from "../components";
import type { AgentInfo, OverviewInfo } from "../lib/api";
import { api } from "../lib/api";
import { LAB_GUIDE_URL } from "../lib/links";

const SERVICES = [
  "runtime",
  "gateway",
  "memory",
  "registry",
  "policy",
  "evaluation",
  "observability",
] as const;

function useAgents(intervalMs = 5000): AgentInfo[] | null {
  const [agents, setAgents] = useState<AgentInfo[] | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .listAgents()
        .then((res) => {
          if (alive) setAgents(res.agents);
        })
        .catch(() => {
          if (alive) setAgents((prev) => prev ?? []); // backend offline — empty state
        });
    load();
    const timer = setInterval(load, intervalMs);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [intervalMs]);
  return agents;
}

function useOverview(intervalMs = 30000): OverviewInfo | null {
  const [info, setInfo] = useState<OverviewInfo | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .getOverview()
        .then((res) => {
          if (alive) setInfo(res);
        })
        .catch(() => {
          /* backend offline — tiles keep last values */
        });
    load();
    const timer = setInterval(load, intervalMs);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [intervalMs]);
  return info;
}

function ageOf(iso: string | null, now: number): string {
  if (!iso) return "—";
  const secs = Math.max(0, Math.floor((now - Date.parse(iso)) / 1000));
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h`;
  return `${Math.floor(secs / 86400)}d`;
}

function stageSummary(agent: AgentInfo): string {
  const stages = agent.deployment?.stages ?? [];
  const failed = stages.find((s) => s.status === "failed");
  if (failed) return `${failed.name} ✕ ${failed.detail}`.slice(0, 60);
  const running = stages.find((s) => s.status === "running");
  if (running) return `${running.name} ◐`;
  const doneCount = stages.filter(
    (s) => s.status === "succeeded" || s.status === "skipped",
  ).length;
  if (doneCount === stages.length && stages.length > 0) return "register ✓";
  return stages.length ? `${doneCount}/${stages.length}` : "—";
}

export function Overview() {
  const { t } = useTranslation();
  const agentsState = useAgents();
  const feedLoading = agentsState === null;
  const agents = agentsState ?? [];
  const info = useOverview();
  const now = Date.now();
  const active = agents.filter((a) => a.status === "active").length;
  const assets = info?.registry_assets;

  return (
    <section>
      <ViewHead
        kicker={t("overview.kicker")}
        title={t("overview.title")}
        meta={t("overview.meta")}
      />

      <a
        className="labcta"
        href={LAB_GUIDE_URL}
        target="_blank"
        rel="noreferrer"
        data-testid="lab-guide-cta"
      >
        <span className="ic">⧉</span>
        <span className="tx">
          <b>{t("overview.lab.title")}</b>
          <span className="sub">{t("overview.lab.sub")}</span>
        </span>
        <span className="go">{t("overview.lab.open")} ↗</span>
      </a>

      <div className="tiles">
        <StatTile
          label={t("overview.tiles.deployedAgents")}
          value={String(active)}
          foot={
            agents.length > active
              ? t("overview.tiles.inFlight", { count: agents.length - active })
              : t("overview.tiles.none")
          }
          style={{ "--i": 0 } as CSSProperties}
        />
        <StatTile
          label={t("overview.tiles.activeSessions")}
          value={info ? String(info.active_sessions) : "—"}
          foot={t("overview.tiles.last24h")}
          style={{ "--i": 1 } as CSSProperties}
        />
        <StatTile
          label={t("overview.tiles.registryAssets")}
          value={assets ? String(assets.total) : "—"}
          foot={
            assets && assets.total > 0
              ? t("overview.tiles.breakdown", {
                  agents: assets.agents,
                  tools: assets.tools,
                  skills: assets.skills,
                })
              : t("overview.tiles.breakdownEmpty")
          }
          style={{ "--i": 2 } as CSSProperties}
        />
        <StatTile
          label={t("overview.tiles.evalPassRate")}
          value={
            info?.eval_pass_rate != null ? `${Math.round(info.eval_pass_rate * 100)}%` : "—"
          }
          foot={
            info && info.eval_runs > 0
              ? t("overview.tiles.runCount", { count: info.eval_runs })
              : t("overview.tiles.noRuns")
          }
          style={{ "--i": 3 } as CSSProperties}
        />
      </div>

      <div className="grid-2">
        <Panel
          brk
          title={t("overview.feed.title")}
          sub={t("overview.feed.sub")}
          pad={false}
          style={{ "--i": 4 } as CSSProperties}
        >
          <DataTable
            columns={[
              { key: "agent", label: t("overview.feed.agent") },
              { key: "method", label: t("overview.feed.method") },
              { key: "stage", label: t("overview.feed.stage") },
              { key: "status", label: t("overview.feed.status") },
              { key: "arn", label: t("overview.feed.runtimeArn") },
              { key: "age", label: t("overview.feed.age") },
            ]}
            isEmpty={agents.length === 0}
            empty={
              feedLoading ? (
                <span className="loading-line" style={{ padding: 0 }}>
                  {t("common.loading")}
                </span>
              ) : (
                <Link to="/create" style={{ color: "var(--ink-3)" }}>
                  {t("overview.feed.empty")}
                </Link>
              )
            }
          >
            {agents.map((agent) => (
              <tr key={agent.id}>
                <td className="pri">{agent.name}</td>
                <td>
                  <MethodChip method={agent.method} />
                </td>
                <td className="mono dim">{stageSummary(agent)}</td>
                <td>
                  {agent.status === "active" ? (
                    <Chip tone="good" icon="●">
                      {t("status.active")}
                    </Chip>
                  ) : agent.status === "failed" ? (
                    <Chip tone="crit" icon="✕">
                      {t("status.failed")}
                    </Chip>
                  ) : (
                    <Chip tone="warn" icon="◐">
                      {t("status.deploying")}
                    </Chip>
                  )}
                </td>
                <td>
                  <span className="arn">{agent.arn ?? "—"}</span>
                </td>
                <td className="mono dim">{ageOf(agent.created_at, now)}</td>
              </tr>
            ))}
          </DataTable>
        </Panel>

        <Panel
          title={t("overview.health.title")}
          sub={t("overview.health.sub")}
          pad={false}
          style={{ "--i": 5 } as CSSProperties}
        >
          <div className="health">
            {SERVICES.map((svc) => {
              const ready = svc === "runtime" ? active > 0 : Boolean(info?.services[svc]);
              const detail = svc === "runtime" ? "" : (info?.service_detail[svc] ?? "");
              return (
                <div className="row" key={svc}>
                  <span className={`led ${ready ? "g" : "off"}`}></span>
                  <span className="nm">{t(`overview.health.${svc}`)}</span>
                  <span className="st">
                    {svc === "runtime" && active > 0
                      ? t("overview.health.activeCount", { count: active })
                      : ready
                        ? `${t("overview.health.ready")}${detail ? ` · ${detail}` : ""}`
                        : t("overview.health.pending")}
                  </span>
                </div>
              );
            })}
          </div>
        </Panel>
      </div>
    </section>
  );
}

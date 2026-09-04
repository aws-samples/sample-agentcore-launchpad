import type { CSSProperties } from "react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  Chip,
  DataTable,
  LoadError,
  MethodChip,
  Panel,
  StatTile,
  ViewHead,
} from "../components";
import type { AgentInfo, OnlineQuality, OverviewInfo } from "../lib/api";
import { api, errorMessage } from "../lib/api";
import { LAB_GUIDE_URL } from "../lib/links";

// Two kinds of health row, and they need different empty states:
//   "bootstrap" — the resource comes from `make bootstrap` (config ids, or the
//     account's Transaction Search destination). Missing = something to fix.
//   "usage"     — the row counts what the operator has created (deployed agents,
//     completed eval runs, or an explicitly attached Policy Engine). Missing =
//     the expected state of a fresh, healthy account, so it must not read as a
//     fault (ISSUE-001).
// Adding a row here forces the choice; `usage` rows name the action that lights
// them up via `overview.health.creates.<svc>`.
const SERVICES = [
  { id: "runtime", kind: "usage" },
  { id: "gateway", kind: "bootstrap" },
  { id: "memory", kind: "bootstrap" },
  { id: "registry", kind: "bootstrap" },
  { id: "policy", kind: "usage" },
  { id: "evaluation", kind: "usage" },
  { id: "observability", kind: "bootstrap" },
] as const;

interface AgentsState {
  /** `null` until the first answer — never coerced to `[]` on failure. */
  agents: AgentInfo[] | null;
  /** Copy for the last failed poll; cleared by the next success. */
  error: string | null;
  retry: () => void;
}

function useAgents(intervalMs = 5000): AgentsState {
  const [agents, setAgents] = useState<AgentInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // bumping `tick` restarts the effect: an immediate fetch + a fresh interval
  const [tick, setTick] = useState(0);
  const retry = useCallback(() => setTick((n) => n + 1), []);
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .listAgents()
        .then((res) => {
          if (!alive) return;
          setAgents(res.agents);
          setError(null);
        })
        .catch((err: unknown) => {
          // Rows already loaded stay on screen (the topbar chip reports the
          // outage); an empty ledger is only claimed after a 200.
          if (alive) setError(errorMessage(err));
        });
    load();
    const timer = setInterval(load, intervalMs);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [intervalMs, tick]);
  return { agents, error, retry };
}

interface OverviewState {
  info: OverviewInfo | null;
  error: string | null;
  retry: () => void;
}

function useOverview(intervalMs = 30000): OverviewState {
  const [info, setInfo] = useState<OverviewInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const retry = useCallback(() => setTick((n) => n + 1), []);
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .getOverview()
        .then((res) => {
          if (!alive) return;
          setInfo(res);
          setError(null);
        })
        .catch((err: unknown) => {
          // tiles keep their last values through an outage; only a never-
          // loaded overview renders as failed (not as "none yet")
          if (alive) setError(errorMessage(err));
        });
    load();
    const timer = setInterval(load, intervalMs);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [intervalMs, tick]);
  return { info, error, retry };
}

// Separate fetch from /api/overview: this one is a Logs Insights scan (served
// from a 120 s backend cache), so the four ledger-backed tiles must not wait on it.
function useOnlineQuality(intervalMs = 30000): OnlineQuality | null {
  const [quality, setQuality] = useState<OnlineQuality | null>(null);
  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .overviewOnlineQuality()
        .then((res) => {
          if (alive) setQuality(res);
        })
        .catch(() => {
          /* backend offline or query failed — keep the last value */
        });
    load();
    const timer = setInterval(load, intervalMs);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [intervalMs]);
  return quality;
}

// ONLINE QUALITY tile thresholds (the mean is already polarity-normalised, so
// higher is always better): good ≥ 0.8 · warn ≥ 0.5 · crit below.
function qualityColor(mean: number): string {
  return mean >= 0.8
    ? "var(--good)"
    : mean >= 0.5
      ? "var(--warn)"
      : "var(--crit-text)";
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
  const { agents: agentsState, error: agentsError, retry: retryAgents } = useAgents();
  // never answered 2xx and the last attempt failed ⇒ the account is unknown, not empty
  const agentsFailed = agentsState === null && agentsError !== null;
  const feedLoading = agentsState === null && !agentsFailed;
  const agents = agentsState ?? [];
  const { info, error: overviewError, retry: retryOverview } = useOverview();
  const overviewFailed = info === null && overviewError !== null;
  const quality = useOnlineQuality();
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

      <div className="tiles five">
        <StatTile
          label={t("overview.tiles.deployedAgents")}
          value={agentsFailed ? "—" : String(active)}
          foot={
            agentsFailed
              ? t("common.loadFailedShort")
              : agents.length > active
                ? t("overview.tiles.inFlight", { count: agents.length - active })
                : t("overview.tiles.none")
          }
          style={{ "--i": 0 } as CSSProperties}
        />
        <StatTile
          label={t("overview.tiles.activeSessions")}
          value={info ? String(info.active_sessions) : "—"}
          foot={overviewFailed ? t("common.loadFailedShort") : t("overview.tiles.last24h")}
          style={{ "--i": 1 } as CSSProperties}
        />
        <StatTile
          label={t("overview.tiles.registryAssets")}
          value={assets ? String(assets.total) : "—"}
          foot={
            overviewFailed
              ? t("common.loadFailedShort")
              : assets && assets.total > 0
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
            info?.eval_pass_rate != null
              ? `${Math.round(info.eval_pass_rate * 100)}%`
              : "—"
          }
          foot={
            overviewFailed
              ? t("common.loadFailedShort")
              : info && info.eval_runs > 0
                ? t("overview.tiles.runCount", { count: info.eval_runs })
                : t("overview.tiles.noRuns")
          }
          style={{ "--i": 3 } as CSSProperties}
        />
        <div data-testid="overview-online-quality" style={{ display: "contents" }}>
          <StatTile
            label={t("overview.tiles.onlineQuality")}
            value={
              quality == null ? (
                "…"
              ) : quality.mean != null ? (
                <span style={{ color: qualityColor(quality.mean) }}>
                  {`${Math.round(quality.mean * 100)}%`}
                </span>
              ) : (
                "—"
              )
            }
            foot={
              quality == null
                ? t("overview.tiles.last24h")
                : quality.configs === 0 && quality.scores === 0
                  ? t("overview.tiles.onlineQualityNone")
                  : quality.scores === 0
                    ? t("overview.tiles.onlineQualityPending")
                    : t("overview.tiles.onlineQualityFoot", {
                        sessions: quality.sessions,
                        agents: quality.agents,
                      })
            }
            style={{ "--i": 4 } as CSSProperties}
          />
        </div>
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
            error={agentsFailed ? agentsError : null}
            onRetry={retryAgents}
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
          {overviewFailed ? (
            // the rows would otherwise read "NONE YET" for every service — a
            // fresh-account story that is false when /api/overview never answered
            <LoadError
              message={overviewError}
              onRetry={retryOverview}
              data-testid="overview-health-load-error"
            />
          ) : (
            <div className="health">
              {SERVICES.map(({ id: svc, kind }) => {
                const ready =
                  svc === "runtime" ? active > 0 : Boolean(info?.services[svc]);
                const detail =
                  svc === "runtime" ? "" : (info?.service_detail[svc] ?? "");
                // an empty usage row is "you haven't made one yet", not a fault:
                // neutral LED + the action that creates it
                const led = ready ? "g" : kind === "usage" ? "n" : "off";
                return (
                  <div className="row" key={svc} data-testid={`health-${svc}`}>
                    <span className={`led ${led}`}></span>
                    <span className="nm">{t(`overview.health.${svc}`)}</span>
                    <span
                      className={`st${!ready && kind === "usage" ? " dim" : ""}`}
                    >
                      {svc === "runtime" && active > 0
                        ? t("overview.health.activeCount", { count: active })
                        : ready
                          ? `${t("overview.health.ready")}${detail ? ` · ${detail}` : ""}`
                          : kind === "usage"
                            ? `${t("overview.health.notCreated")} · ${t(
                                `overview.health.creates.${svc}`,
                              )}`
                            : t("overview.health.pending")}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </Panel>
      </div>
    </section>
  );
}

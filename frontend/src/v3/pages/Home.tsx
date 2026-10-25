import { ArrowRight, CheckCircle2, RefreshCw } from "lucide-react";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { api, type AnnouncementPage, dlcApi, type InboxInfo, type PublicAnnouncement, type TtfaInfo } from "../../lib/api";
import { loadTasks } from "../../v2/tasks";
import { useWorkspace } from "../../workspace/workspace-context";
import { useHiddenNav } from "../hiddenNav";
import { HOME_SECTIONS } from "../nav";
import { LaunchSequence } from "../onboarding/LaunchSequence";
import { YourTurn } from "../story/YourTurn";
import { type Attention, agentSignal, attentionFor, sortAttention } from "../signals";
import { Chip, Empty, Lamp, Notice, PageHead, Panel, Skeleton, Spark, Stat } from "../ui";
import { useLoad } from "../hooks";
import { ago, ms, pct } from "../format";
import { AdminInbox } from "./home/AdminInbox";
import { AnnouncementsBrief } from "./home/AnnouncementsBrief";
import "./home/home.css";
import { Lifecycle } from "./home/Lifecycle";
import { MoreSignals } from "./home/MoreSignals";
import { RecentTasks } from "./home/RecentTasks";
import { readReleases } from "./home/releases";
import { ServiceHealthPanel } from "./home/ServiceHealthPanel";

const ATTENTION_TO: Record<Attention["kind"], (id: string) => string> = {
  deploy_failed: (id) => `/v3/agents?id=${id}`,
  deploying: (id) => `/v3/agents?id=${id}`,
  release_waiting: (id) => `/v3/gate?agent=${id}`,
  gate_invalid: (id) => `/v3/gate?agent=${id}`,
  gate_blocked: (id) => `/v3/gate?agent=${id}`,
};

/**
 * The workbench. Its first question is not "how is everything" but "what needs me":
 * failed deploys, releases waiting for a signature, gates that blocked or could not
 * decide — ranked, each one a click from where it gets handled. The fleet and the
 * traffic sit underneath, because quiet is the normal state and should read as such.
 * Below them, the V2 Workbench's modules: registry assets, pass rate, online quality and
 * (administrators) time to first agent; the administrator's inbox; recent evaluation
 * tasks and announcements; the Agent lifecycle and service health. Menu settings may
 * hide the launch sequence and "Your turn" for everyone (`HOME_SECTIONS`).
 */
export function V3Home() {
  const { t } = useTranslation();
  const { current } = useWorkspace();
  const ws = current?.id ?? "";
  const agents = useLoad(() => api.listAgents(), `v3-home-agents:${ws}`);
  const list = useMemo(() => (agents.data?.agents ?? []).filter((a) => a.status !== "deleted"), [agents.data]);
  const releaseKey = list.map((a) => `${a.id}:${a.status}:${a.version}:${a.updated_at}`).join(",");
  const rel = useLoad(
    () => readReleases(list, (id) => dlcApi.release(id)), `v3-home-rel:${ws}:${releaseKey}`);
  const dash = useLoad(() => api.obsDashboard("24h"), `v3-home-dash:${ws}`);
  const { isAdmin } = useAuth();
  const { hidden, ready: hiddenReady } = useHiddenNav();
  const showLaunch = hiddenReady && !hidden.has(HOME_SECTIONS.launch);
  const showTurn = hiddenReady && !hidden.has(HOME_SECTIONS.turn);
  const overview = useLoad(() => api.getOverview(), `v3-home-overview:${ws}`);
  const quality = useLoad(() => api.overviewOnlineQuality(), `v3-home-quality:${ws}`);
  // administrator-only reads; a member (or a 403 / older backend) simply gets no tile or inbox
  const ttfa = useLoad<TtfaInfo | null>(
    () => (isAdmin ? api.overviewTtfa() : Promise.resolve(null)), `v3-home-ttfa:${ws}:${isAdmin}`);
  const inbox = useLoad<InboxInfo | null>(
    () => (isAdmin ? api.inbox() : Promise.resolve(null)), `v3-home-inbox:${ws}:${isAdmin}`);
  const tasks = useLoad(loadTasks, `v3-home-tasks:${ws}`);
  // hub-global notices; a failed read just leaves the board out
  const notices = useLoad<AnnouncementPage<PublicAnnouncement>>(() => api.listAnnouncements(5, 0), "v3-home-announcements");

  const queue = useMemo(() => {
    const items = list.flatMap((a) => attentionFor(a, rel.data?.states[a.id]?.pending ?? null));
    return sortAttention(items);
  }, [list, rel.data]);

  const live = list.filter((a) => a.status === "active").length;
  const needsYou = queue.filter((q) => q.signal === "act").length;
  const waiting = queue.filter((q) => q.signal === "wait").length;
  const tiles = dash.data?.tiles;
  const hasTraffic = Boolean(tiles && tiles.traces.total > 0);
  const series = dash.data?.series ?? [];
  const checking = (agents.loading && !agents.data) || (rel.loading && !rel.data);
  const incomplete = Boolean(agents.error || rel.error || rel.data?.unavailable || rel.data?.unchecked);
  const refresh = () => {
    agents.reload(); rel.reload(); dash.reload();
    overview.reload(); quality.reload(); ttfa.reload(); inbox.reload(); tasks.reload(); notices.reload();
  };
  const hasNotices = (notices.data?.announcements.length ?? 0) > 0;

  const headline =
    checking
      ? t("v3.home.loading")
      : incomplete && needsYou + waiting === 0
        ? t("v3.ux.incomplete")
      : needsYou + waiting === 0
        ? t("v3.home.quiet")
        : t("v3.home.needs", { count: needsYou + waiting });

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 18 }}>
      <PageHead
        eyebrow={t("v3.home.eyebrow", { ws: current ? `${current.name} · ${current.region}` : "—" })}
        title={headline}
        sub={t("v3.home.sub")}
        end={<><button type="button" className="v3-btn" onClick={refresh} disabled={agents.loading || rel.loading || dash.loading}>
          <RefreshCw size={14} /> {t("v3.ux.refresh")}
        </button><Link to="/v3/create" className="v3-btn primary">{t("v3.nav.create")}</Link></>}
      />
      {agents.error && <Notice s="act">{t("v3.ux.agentsFailed")} {agents.error}</Notice>}
      {incomplete && !agents.error && <Notice s="wait">{t("v3.ux.releaseCoverage", {
        count: (rel.data?.unavailable ?? 0) + (rel.data?.unchecked ?? 0),
      })} <Link to="/v3/gate" className="v3-inline-link">{t("v3.agent.openGate")}</Link></Notice>}

      <div className="v3-grid c4" data-tour="stats">
        <Panel signal="ok">
          <Stat
            label={t("v3.home.live")}
            value={agents.data ? live : "—"}
            signal={live ? "ok" : undefined}
            foot={t("v3.home.ofTotal", { count: list.length })}
          />
        </Panel>
        <Panel signal={waiting ? "wait" : undefined}>
          <Stat label={t("v3.home.waiting")} value={!checking && !incomplete ? waiting : "—"} signal={waiting ? "wait" : undefined}
            foot={t("v3.home.waitingFoot")} />
        </Panel>
        <Panel signal={needsYou ? "act" : undefined}>
          <Stat label={t("v3.home.needsYou")} value={!checking && !incomplete ? needsYou : "—"} signal={needsYou ? "act" : undefined}
            foot={t("v3.home.needsYouFoot")} />
        </Panel>
        <Panel>
          <Stat
            label={t("v3.home.p95")}
            // no traces is "no measurement", not a 0ms latency
            value={tiles && hasTraffic ? ms(tiles.latency.p95_ms) : "—"}
            foot={tiles && hasTraffic ? t("v3.home.errorRate", { rate: pct(tiles.error_rate, 1) }) : t("v3.home.noTraffic")}
            signal={tiles && hasTraffic && tiles.error_rate > 0.05 ? "act" : undefined}
          />
        </Panel>
      </div>

      <MoreSignals overview={overview.data} quality={quality.data} ttfa={ttfa.data} isAdmin={isAdmin} />

      {agents.data && showLaunch && (
        <LaunchSequence workspace={current ?? null} agents={list} releases={rel.data?.states ?? null} />
      )}
      {/* past the opening moves, the storyline's turns: mine and the ones I wait on (storyline-ux §3.4) */}
      {list.length > 0 && showTurn && <YourTurn refresh={releaseKey} />}
      {isAdmin && <AdminInbox inbox={inbox.data} />}

      <div className="v3-grid v3-split">
        <div data-tour="queue">
        <Panel title={t("v3.home.queue")} end={<span className="mono">{queue.length}</span>} flush>
          {checking ? (
            <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
          ) : queue.length === 0 ? (
            <div className="v3-empty">
              {!incomplete && <CheckCircle2 size={30} style={{ color: "var(--v3-ok)" }} aria-hidden="true" />}
              <strong>{t(incomplete ? "v3.ux.incomplete" : "v3.home.queueEmpty")}</strong>
              <span>{t(incomplete ? "v3.ux.retryHint" : "v3.home.queueEmptySub")}</span>
            </div>
          ) : (
            <table className="v3-table">
              <tbody>
                {queue.map((item) => (
                  <tr key={item.key}>
                    <td style={{ width: 30 }}><Lamp s={item.signal} live={item.kind === "deploying"} /></td>
                    <td>
                      <div style={{ fontWeight: 600 }}>{t(`v3.attention.${item.kind}`)}</div>
                      <div className="mono" style={{ color: "var(--v3-text-3)", fontSize: 12 }}>{item.agentName}</div>
                    </td>
                    <td className="num" style={{ color: "var(--v3-text-3)", width: 70 }}>{ago(item.at)}</td>
                    <td style={{ width: 120, textAlign: "right" }}>
                      <Link to={ATTENTION_TO[item.kind](item.agentId)} className="v3-btn sm">
                        {t(`v3.attention.${item.kind}Act`)} <ArrowRight size={13} />
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
        </div>

        <Panel title={t("v3.home.traffic")} end={<span className="mono">24h</span>}>
          {dash.loading && !dash.data ? (
            <Skeleton rows={3} />
          ) : dash.error ? (
            <Notice s="act">{t("v3.ux.trafficFailed")} {dash.error}
              <button type="button" className="v3-btn sm" onClick={dash.reload}>{t("v3.ux.retry")}</button>
            </Notice>
          ) : !tiles || !hasTraffic || series.length < 2 ? (
            <Empty title={t("v3.home.noTraffic")}>{t("v3.home.noTrafficSub")}</Empty>
          ) : (
            <div style={{ display: "grid", gap: 16 }}>
              <Spark values={series.map((b) => b.traces)} />
              <div className="v3-grid c3" style={{ gap: 10 }}>
                <Stat label={t("v3.home.traces")} value={tiles.traces.total.toLocaleString()} />
                <Stat label={t("v3.home.sessions")} value={tiles.sessions.total.toLocaleString()} />
                <Stat
                  label={t("v3.home.spend")}
                  value={tiles.tokens.est_cost_usd === null ? "—" : `$${tiles.tokens.est_cost_usd.toFixed(2)}`}
                />
              </div>
              {series.some((b) => b.errors > 0) && (
                <div>
                  <div className="v3-stat"><div className="label">{t("v3.home.errors")}</div></div>
                  <Spark values={series.map((b) => b.errors)} s="act" />
                </div>
              )}
            </div>
          )}
        </Panel>
      </div>

      <Panel
        title={t("v3.home.fleet")}
        end={<Link to="/v3/agents" className="v3-btn ghost sm">{t("v3.home.allAgents")} <ArrowRight size={13} /></Link>}
        flush
      >
        {agents.loading && !agents.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={5} /></div>
        ) : list.length === 0 ? (
          <Empty title={t("v3.home.noAgents")}>
            <Link to="/v3/create" className="v3-btn primary">{t("v3.nav.create")}</Link>
          </Empty>
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v3.agents.name")}</th>
                <th>{t("v3.agents.method")}</th>
                <th className="num">{t("v3.agents.version")}</th>
                <th>{t("v3.agents.release")}</th>
                <th className="num">{t("v3.agents.updated")}</th>
              </tr>
            </thead>
            <tbody>
              {list.slice(0, 8).map((a) => {
                const state = rel.data?.states[a.id]?.state;
                return (
                  <tr key={a.id}>
                    <td style={{ width: 30 }}><Lamp s={agentSignal(a)} live={a.status === "active"} /></td>
                    <td>
                      <div className="v3-name"><div><Link className="v3-inline-link" to={`/v3/agents?id=${a.id}`}><b>{a.display_name || a.name}</b></Link><small>{a.name}</small></div></div>
                    </td>
                    <td><Chip>{a.method}</Chip></td>
                    <td className="num">v{a.version ?? "—"}</td>
                    <td>
                      {state?.endpoint_mode === "live" ? (
                        <Chip s="ok">live {state.live_version ?? "—"}
                          {state.candidate_version && state.candidate_version !== state.live_version
                            ? ` · cand ${state.candidate_version}` : ""}</Chip>
                      ) : state ? (
                        <Chip>{t("v3.agents.ungated")}</Chip>
                      ) : (
                        <Chip>{t("v3.ux.unknown")}</Chip>
                      )}
                    </td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(a.updated_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>

      <div className="v3-grid v3-split">
        <Lifecycle />
        <ServiceHealthPanel info={overview.data} error={overview.error} onRetry={overview.reload}
          activeAgents={agents.data ? live : null} />
      </div>

      <div className={hasNotices ? "v3-grid v3-split" : "v3-grid"}>
        <RecentTasks tasks={tasks.data} loading={tasks.loading} error={tasks.error} onRetry={tasks.reload} />
        {hasNotices && <AnnouncementsBrief page={notices.data} isAdmin={isAdmin} />}
      </div>
    </div>
  );
}

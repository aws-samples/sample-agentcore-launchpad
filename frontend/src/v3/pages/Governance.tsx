import "./governance.css";

import { ArrowRight, Play, Plus, RefreshCw, Search, ShieldCheck } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import {
  api,
  type GovernanceDecisionResponse,
  type GovernanceGatewaySummary,
  type GovernanceToolInfo,
} from "../../lib/api";
import { type ExternalMcpTarget, type LambdaToolTarget, listExternalTargets, listLambdaTargets } from "../../lib/api/mcpReviews";
import { ApiErrorDetail } from "../../components/ApiErrorDetail";
import { governanceError, governanceStatusLevel } from "../../lib/governance";
import { useWorkspace } from "../../workspace/workspace-context";
import { ago } from "../format";
import { useLoad } from "../hooks";
import { Btn, Chip, Dialog, Empty, Filters, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";
import { GatewayToolTargets } from "./tools/GatewayToolTargets";

type Tab = "gateways" | "tools" | "targets";
const TABS: readonly Tab[] = ["gateways", "tools", "targets"];
type EngineFilter = "all" | "attached" | "enforce" | "logOnly" | "none";

const CODE_DEMO = "import math\nprint('sqrt(1764) =', math.isqrt(1764))";

/** An AWS / governance status string as a Signal. ENFORCE is a state, not a verdict. */
function govSignal(status: string | null | undefined): Signal {
  if (status?.toUpperCase() === "ENFORCE") return "info";
  const level = governanceStatusLevel(status);
  return level === "good" ? "ok" : level === "crit" ? "act" : level === "warn" ? "wait" : "off";
}

function engineState(g: GovernanceGatewaySummary): Exclude<EngineFilter, "all" | "attached"> {
  const engine = g.policy_engine;
  if (!engine || engine.missing) return "none";
  return engine.mode === "ENFORCE" ? "enforce" : "logOnly";
}

// gateway sub-pages stay hosted from V2 (detail, policies, rate limits, decisions, audit)
const gatewayUrl = (id: string, section?: string) =>
  `/v2/governance?view=gateway&gateway=${encodeURIComponent(id)}${section ? `&section=${section}` : ""}`;

export interface Attention {
  key: string;
  s: Signal;
  title: string;
  detail: string;
  to: string;
  act: string;
}

const TARGETS_URL = "/v3/governance?tab=targets";

/** The tool targets (Lambda and adopted external) whose schema waits for a reviewer,
 *  and the Lambda targets whose publish failed. */
export function targetAttentionOf(
  lambdas: LambdaToolTarget[],
  external: ExternalMcpTarget[],
  t: (k: string, o?: Record<string, unknown>) => string,
): Attention[] {
  const out: Attention[] = [];
  const failed = lambdas.filter((x) => x.status === "failed");
  if (failed.length) {
    out.push({
      key: "targets:failed", s: "act", title: t("v3.governance.att.targetsFailed", { count: failed.length }),
      detail: failed.map((x) => x.target_name).join("  ·  "), to: TARGETS_URL, act: t("v3.governance.att.inspect"),
    });
  }
  const pending = [
    ...lambdas.filter((x) => x.review.state === "reviewing").map((x) => x.target_name),
    ...external.filter((x) => x.review?.state === "reviewing").map((x) => x.target_name),
  ];
  if (pending.length) {
    out.push({
      key: "targets:review", s: "wait", title: t("v3.governance.att.targetsReview", { count: pending.length }),
      detail: pending.join("  ·  "), to: TARGETS_URL, act: t("v3.governance.att.review"),
    });
  }
  return out;
}

/** What on the gateway inventory needs someone, most urgent first. */
function attentionOf(
  gateways: GovernanceGatewaySummary[],
  t: (k: string, o?: Record<string, unknown>) => string,
  extra: Attention[] = [],
): Attention[] {
  const out: Attention[] = [...extra];
  for (const g of gateways) {
    const s = govSignal(g.status);
    if (s === "act" || s === "wait") {
      out.push({
        key: `${g.id}:status`, s, title: t("v3.governance.att.gatewayNotReady", { name: g.name }),
        detail: g.status_reasons.join("; ") || g.status, to: gatewayUrl(g.id), act: t("v3.governance.att.inspect"),
      });
    }
    if (g.policy_engine?.missing) {
      out.push({
        key: `${g.id}:engine`, s: "act", title: t("v3.governance.att.engineMissing", { name: g.name }),
        detail: g.policy_engine.id, to: gatewayUrl(g.id, "policies"), act: t("v3.governance.att.fix"),
      });
    } else if (g.policy_engine?.mode === "LOG_ONLY") {
      out.push({
        key: `${g.id}:logonly`, s: "wait", title: t("v3.governance.att.logOnly", { name: g.name }),
        detail: t("v3.governance.att.logOnlyDetail"), to: gatewayUrl(g.id, "decisions"), act: t("v3.governance.att.review"),
      });
    }
    const bad = g.targets.filter((tg) => govSignal(tg.status) === "act" || govSignal(tg.status) === "wait");
    if (bad.length) {
      out.push({
        key: `${g.id}:targets`, s: bad.some((tg) => govSignal(tg.status) === "act") ? "act" : "wait",
        title: t("v3.governance.att.targets", { name: g.name, count: bad.length }),
        detail: bad.map((tg) => `${tg.name} · ${tg.status}`).join("  ·  "), to: gatewayUrl(g.id, "targets"), act: t("v3.governance.att.inspect"),
      });
    }
  }
  const rank: Record<Signal, number> = { act: 0, wait: 1, info: 2, ok: 3, off: 4 };
  return out.sort((a, b) => rank[a.s] - rank[b.s]);
}

/** Allow / deny evidence of one gateway over the last 24h; denials lead. */
function DecisionRow({ gateway }: { gateway: GovernanceGatewaySummary }) {
  const { t } = useTranslation();
  const ev = useLoad<GovernanceDecisionResponse>(
    () => api.governanceDecisions(gateway.id, "24h").catch((err: unknown) => Promise.reject(new Error(governanceError(err)))),
    `v3-gov-dec:${gateway.id}`,
  );
  const d = ev.data;
  const total = d ? d.totals.allow + d.totals.deny : 0;
  const denied = d ? [...d.by_tool].filter((r) => r.deny > 0).sort((a, b) => b.deny - a.deny).slice(0, 3) : [];
  const s: Signal = !d ? "off" : !d.available ? "wait" : d.totals.deny > 0 ? "act" : total > 0 ? "ok" : "off";
  return (
    <tr>
      <td style={{ width: 30 }}><Lamp s={s} /></td>
      <td>
        <div className="v3-name">
          <div>
            <b>{gateway.name}</b>
            <small>{gateway.policy_engine?.name ?? gateway.policy_engine?.id} · {gateway.policy_engine?.mode ?? "—"}</small>
          </div>
        </div>
      </td>
      <td>
        {ev.loading && !d ? (
          <span className="v3-gov-muted">…</span>
        ) : ev.error ? (
          <span className="v3-gov-err" title={ev.error}>{t("v3.governance.dec.unreadable")}</span>
        ) : d && !d.available ? (
          <span className="v3-gov-muted" title={d.unavailable_reason ?? undefined}>{t("v3.governance.dec.noChannel")}</span>
        ) : d ? (
          <div className="v3-gov-bar" title={`${d.totals.allow} / ${d.totals.deny}`}>
            <i className="allow" style={{ flexGrow: d.totals.allow || (total ? 0 : 1) }} />
            <i className="deny" style={{ flexGrow: d.totals.deny }} />
          </div>
        ) : null}
      </td>
      <td className="num">{d?.available ? d.totals.allow : "—"}</td>
      <td className="num" style={d && d.totals.deny > 0 ? { color: "var(--v3-act)" } : undefined}>{d?.available ? d.totals.deny : "—"}</td>
      <td className="mono" style={{ color: "var(--v3-text-2)" }}>
        {denied.length ? denied.map((r) => `${r.tool} ×${r.deny}`).join("  ·  ") : "—"}
      </td>
      <td style={{ width: 1, whiteSpace: "nowrap" }}>
        <Link className="v3-btn sm ghost" to={gatewayUrl(gateway.id, "decisions")}>{t("v3.governance.dec.open")} <ArrowRight size={13} /></Link>
      </td>
    </tr>
  );
}

function GatewaysTab({ data, loading, error, targetAttention }: {
  data: { gateways: GovernanceGatewaySummary[]; account_id?: string | null; region?: string } | null;
  loading: boolean;
  /** The failure (an ApiError or its text); null ⇒ the load did not fail. */
  error: unknown;
  /** The tool-target items of 需要处理 (`targetAttentionOf`). */
  targetAttention: Attention[];
}) {
  const { t } = useTranslation();
  const [managed, setManaged] = useState<"all" | "managed" | "unmanaged">("all");
  const [engine, setEngine] = useState<EngineFilter>("all");
  const [q, setQ] = useState("");
  const gateways = useMemo(() => data?.gateways ?? [], [data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return gateways.filter((g) => {
      if (managed !== "all" && g.managed !== (managed === "managed")) return false;
      if (engine === "attached" && engineState(g) === "none") return false;
      if (engine !== "all" && engine !== "attached" && engineState(g) !== engine) return false;
      return !needle || `${g.name} ${g.id} ${g.description}`.toLowerCase().includes(needle);
    });
  }, [gateways, managed, engine, q]);
  const count = (fn: (g: GovernanceGatewaySummary) => boolean) => gateways.filter(fn).length;
  const attention = useMemo(() => attentionOf(gateways, t, targetAttention), [gateways, t, targetAttention]);
  const withEngine = gateways.filter((g) => g.policy_engine && !g.policy_engine.missing);
  const notReady = count((g) => govSignal(g.status) !== "ok");
  const source = [data?.account_id, data?.region].filter(Boolean).join(" / ");

  if (loading && !data) return <Panel><Skeleton rows={6} /></Panel>;
  if (error != null && !data) return <Notice s="act"><ApiErrorDetail error={error} /></Notice>;

  return (
    <>
      <div className="v3-grid c4">
        <Panel><Stat label={t("v3.governance.kpi.gateways")} value={gateways.length} foot={source || undefined} /></Panel>
        <Panel signal={count((g) => engineState(g) === "enforce") ? "info" : undefined}>
          <Stat label={t("v3.governance.kpi.enforcing")} value={count((g) => engineState(g) === "enforce")}
            foot={t("v3.governance.kpi.enforcingFoot", { count: count((g) => engineState(g) === "logOnly") })} />
        </Panel>
        <Panel signal={notReady ? "act" : gateways.length ? "ok" : undefined}>
          <Stat label={t("v3.governance.kpi.notReady")} value={notReady} signal={notReady ? "act" : undefined}
            foot={t("v3.governance.kpi.notReadyFoot")} />
        </Panel>
        <Panel><Stat label={t("v3.governance.kpi.attachable")} value={count((g) => g.attachability.attachable)}
          foot={t("v3.governance.kpi.managedFoot", { count: count((g) => g.managed) })} /></Panel>
      </div>

      <Panel title={t("v3.governance.attention")} signal={attention.some((a) => a.s === "act") ? "act" : attention.length ? "wait" : "ok"}
        flush={attention.length > 0} end={<span className="mono">{attention.length}</span>}>
        {attention.length === 0 ? (
          <p style={{ margin: 0, color: "var(--v3-text-3)" }}>{t("v3.governance.allClear")}</p>
        ) : (
          <table className="v3-table">
            <tbody>
              {attention.map((a) => (
                <tr key={a.key}>
                  <td style={{ width: 30 }}><Lamp s={a.s} live={a.s === "act"} /></td>
                  <td><div className="v3-name"><div><b>{a.title}</b><small>{a.detail}</small></div></div></td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}><Link className="v3-btn sm" to={a.to}>{a.act} <ArrowRight size={13} /></Link></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <Panel title={t("v3.governance.dec.title")} flush={withEngine.length > 0}
        end={<span style={{ color: "var(--v3-text-3)" }}>{t("v3.governance.dec.window")}</span>}>
        {withEngine.length === 0 ? (
          <p style={{ margin: 0, color: "var(--v3-text-3)" }}>{t("v3.governance.dec.noEngine")}</p>
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v3.governance.col.gateway")}</th>
                <th style={{ width: "22%" }}>{t("v3.governance.dec.split")}</th>
                <th className="num">{t("v3.governance.dec.allow")}</th>
                <th className="num">{t("v3.governance.dec.deny")}</th>
                <th>{t("v3.governance.dec.topDenied")}</th>
                <th />
              </tr>
            </thead>
            <tbody>{withEngine.map((g) => <DecisionRow key={g.id} gateway={g} />)}</tbody>
          </table>
        )}
      </Panel>

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={managed}
          onChange={setManaged}
          options={[
            { value: "all", label: t("v3.governance.f.all"), count: gateways.length },
            { value: "managed", label: t("v3.governance.managed"), count: count((g) => g.managed) },
            { value: "unmanaged", label: t("v3.governance.unmanaged"), count: count((g) => !g.managed) },
          ]}
        />
        <span style={{ width: 1, height: 20, background: "var(--v3-line)" }} aria-hidden="true" />
        <Filters
          value={engine}
          onChange={setEngine}
          options={[
            { value: "all", label: t("v3.governance.f.anyEngine") },
            { value: "enforce", label: "ENFORCE", s: "info", count: count((g) => engineState(g) === "enforce") },
            { value: "logOnly", label: "LOG_ONLY", s: "wait", count: count((g) => engineState(g) === "logOnly") },
            { value: "none", label: t("v3.governance.noEngine"), s: "off", count: count((g) => engineState(g) === "none") },
          ]}
        />
        <div style={{ marginLeft: "auto", position: "relative", flex: "1 1 200px", maxWidth: 300 }}>
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("v3.governance.search")} aria-label={t("v3.governance.search")} />
        </div>
      </div>

      <Panel flush>
        {rows.length === 0 ? (
          <Empty title={gateways.length ? t("v3.governance.noMatch") : t("v3.governance.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v3.governance.col.gateway")}</th>
                <th>{t("v3.governance.col.status")}</th>
                <th>{t("v3.governance.col.targets")}</th>
                <th>{t("v3.governance.col.engine")}</th>
                <th>{t("v3.governance.col.registry")}</th>
                <th>{t("v3.governance.col.managed")}</th>
                <th className="num">{t("v3.governance.col.updated")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((g) => {
                const s = govSignal(g.status);
                const ready = g.targets.filter((tg) => govSignal(tg.status) === "ok").length;
                return (
                  <tr key={g.id}>
                    <td style={{ width: 30 }}><Lamp s={s} live={s === "ok"} /></td>
                    <td>
                      <div className="v3-name">
                        <div>
                          <Link to={gatewayUrl(g.id)} className="v3-gov-link"><b>{g.name}</b></Link>
                          <small title={g.arn}>{g.id}</small>
                        </div>
                      </div>
                    </td>
                    <td>
                      <Chip s={s === "off" ? undefined : s} title={g.status_reasons.join("; ") || undefined}>{g.status}</Chip>
                      <div className="v3-gov-sub mono">{g.authorizer_type}</div>
                    </td>
                    <td title={g.targets.map((tg) => `${tg.name} · ${tg.status}`).join("\n")}>
                      <span className="mono" style={{ color: ready === g.target_count ? "var(--v3-text)" : "var(--v3-wait)" }}>{ready}/{g.target_count}</span>
                      <div className="v3-gov-sub">{g.targets.map((tg) => tg.name).join(", ") || "—"}</div>
                    </td>
                    <td>
                      {g.policy_engine?.missing ? (
                        <Chip s="act" title={g.policy_engine.id}>{t("v3.governance.engineDeleted")}</Chip>
                      ) : g.policy_engine ? (
                        <>
                          <Chip s={govSignal(g.policy_engine.mode)}>{g.policy_engine.mode ?? g.policy_engine.status}</Chip>
                          <div className="v3-gov-sub mono">{g.policy_engine.name}</div>
                        </>
                      ) : (
                        <span className="v3-gov-muted">{t("v3.governance.noEngine")}</span>
                      )}
                    </td>
                    <td style={{ whiteSpace: "nowrap" }}>
                      {g.registry_record ? <Chip s={govSignal(g.registry_record.status)}>{g.registry_record.status}</Chip> : <span className="v3-gov-muted">{t("v3.governance.notCataloged")}</span>}
                      <div className="v3-gov-sub" title={g.attachability.reason ?? undefined}>
                        {g.attachability.attachable ? t("v3.governance.attachable") : t("v3.governance.catalogOnly")}
                      </div>
                    </td>
                    <td>{g.managed ? <Chip s="ok">{t("v3.governance.managed")}</Chip> : <span className="v3-gov-muted">{t("v3.governance.unmanaged")}</span>}</td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(g.updated_at)}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }}>
                      <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                        <Link className="v3-btn sm ghost" to={gatewayUrl(g.id, "policies")}><ShieldCheck size={13} /> {t("v3.governance.policies")}</Link>
                        <Link className="v3-btn sm" to={gatewayUrl(g.id)}>{t("v3.governance.open")}</Link>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
    </>
  );
}

/** Built-in Code Interpreter: run a Python snippet in a fresh sandbox session. */
function CodeInterpreterDemo() {
  const { t } = useTranslation();
  const [code, setCode] = useState(CODE_DEMO);
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<{ ok: true; text: string } | { ok: false; error: unknown } | null>(null);
  const run = async () => {
    setBusy(true);
    setOut(null);
    try {
      const r = await api.runCodeInterpreterDemo(code);
      setOut({ ok: true, text: `${r.stdout}\n— session ${r.session_id} · ${r.latency_ms}ms` });
    } catch (error) {
      setOut({ ok: false, error });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel title={t("v3.governance.tools.ciTitle")} end={
      <Btn size="sm" kind="primary" busy={busy} disabled={!code.trim()} disabledReason={t("story.reason.fillCode")} onClick={() => void run()}>
        <Play size={13} /> {busy ? t("v3.governance.tools.running") : t("v3.governance.tools.run")}
      </Btn>
    }>
      <textarea className="v3-input mono" rows={7} value={code} maxLength={4000} disabled={busy} spellCheck={false}
        aria-label={t("v3.governance.tools.pythonCode")} onChange={(e) => setCode(e.target.value)} />
      {out?.ok && <pre className="v3-pre" style={{ marginTop: 12, color: "var(--v3-ok)" }}>{out.text}</pre>}
      {out && !out.ok && <div style={{ marginTop: 12 }}><Notice s="act"><ApiErrorDetail error={out.error} /></Notice></div>}
    </Panel>
  );
}

function ToolsTab() {
  const { t } = useTranslation();
  const catalog = useLoad(() => api.governanceToolCatalog(), "v3-gov-tools");
  const [source, setSource] = useState<"all" | "gateway" | "builtin">("all");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<GovernanceToolInfo | null>(null);
  const tools = useMemo(() => catalog.data?.tools ?? [], [catalog.data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return tools.filter((tool) => (source === "all" || tool.source === source)
      && (!needle || `${tool.name} ${tool.description} ${tool.target ?? ""}`.toLowerCase().includes(needle)));
  }, [tools, source, q]);
  const targets = new Set(tools.map((tool) => tool.target).filter(Boolean)).size;

  return (
    <>
      <div className="v3-grid c4">
        <Panel><Stat label={t("v3.governance.tools.total")} value={catalog.data ? tools.length : "—"} /></Panel>
        <Panel><Stat label={t("v3.governance.tools.gateway")} value={catalog.data ? tools.filter((x) => x.source === "gateway").length : "—"}
          foot={t("v3.governance.tools.targetsFoot", { count: targets })} /></Panel>
        <Panel><Stat label={t("v3.governance.tools.builtin")} value={catalog.data ? tools.filter((x) => x.source === "builtin").length : "—"} /></Panel>
        <Panel signal={catalog.data ? (catalog.data.gateway_url ? "ok" : "wait") : undefined}>
          <Stat label={t("v3.governance.tools.gatewayState")} value={catalog.data ? (catalog.data.gateway_url ? t("v3.governance.tools.online") : t("v3.governance.tools.offline")) : "—"}
            foot={catalog.data?.gateway_url ? "launchpad-gw / MCP" : undefined} />
        </Panel>
      </div>

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={source}
          onChange={setSource}
          options={[
            { value: "all", label: t("v3.governance.f.all"), count: tools.length },
            { value: "gateway", label: "Gateway", count: tools.filter((x) => x.source === "gateway").length },
            { value: "builtin", label: t("v3.governance.tools.builtin"), count: tools.filter((x) => x.source === "builtin").length },
          ]}
        />
        <div style={{ marginLeft: "auto", position: "relative", flex: "1 1 200px", maxWidth: 300 }}>
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("v3.governance.tools.search")} aria-label={t("v3.governance.tools.search")} />
        </div>
      </div>

      <Panel flush>
        {catalog.loading && !catalog.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={5} /></div>
        ) : catalog.error ? (
          <div style={{ padding: 20 }}><Notice s="act"><ApiErrorDetail error={catalog.failure} /></Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("v3.governance.tools.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th>{t("v3.governance.tools.colName")}</th>
                <th>{t("v3.governance.tools.colSource")}</th>
                <th>{t("v3.governance.tools.colDesc")}</th>
                <th>{t("v3.governance.tools.colAuth")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((tool) => (
                <tr key={tool.name} className="click" onClick={() => setSelected(tool)}>
                  <td className="mono"><b>{tool.name}</b></td>
                  <td><Chip s={tool.source === "gateway" ? "info" : undefined}>{tool.source === "gateway" ? "Gateway" : t("v3.governance.tools.builtin")}{tool.target ? ` · ${tool.target}` : ""}</Chip></td>
                  <td style={{ color: "var(--v3-text-2)" }}><span className="v3-gov-clip">{tool.description || "—"}</span></td>
                  <td className="mono" style={{ color: "var(--v3-text-3)" }}>{tool.auth}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <div className="v3-grid c2" style={{ alignItems: "start" }}>
        <CodeInterpreterDemo />
        <Panel title={t("v3.governance.tools.brTitle")}>
          <p style={{ margin: "0 0 14px", color: "var(--v3-text-2)" }}>{t("v3.governance.tools.brSub")}</p>
          <Link className="v3-btn" to="/v2/governance?tab=tools&full=1">{t("v3.governance.tools.brOpen")} <ArrowRight size={13} /></Link>
        </Panel>
      </div>

      {selected && (
        <Dialog wide title={selected.name} onClose={() => setSelected(null)}
          foot={<Btn kind="ghost" onClick={() => setSelected(null)}>{t("v3.governance.close")}</Btn>}>
          <dl className="v3-kv">
            <dt>{t("v3.governance.tools.colSource")}</dt><dd>{selected.source}</dd>
            <dt>{t("v3.governance.tools.target")}</dt><dd className="mono">{selected.target ?? "—"}</dd>
            <dt>{t("v3.governance.tools.colAuth")}</dt><dd className="mono">{selected.auth}</dd>
            <dt>{t("v3.governance.tools.colDesc")}</dt><dd style={{ whiteSpace: "pre-wrap" }}>{selected.description || "—"}</dd>
          </dl>
          <div className="v3-gov-subtitle">{t("v3.governance.tools.schema")}</div>
          <pre className="v3-pre">{JSON.stringify(selected.inputSchema ?? null, null, 2)}</pre>
        </Dialog>
      )}
    </>
  );
}

/**
 * Gateway governance — who may call what through the MCP Gateways: the gateway
 * inventory (status, targets, Policy Engine mode, Registry publication) led by what
 * needs attention and the last day's denials; the tool catalog with the built-in
 * tool demos; and the tool targets (`?tab=targets`: Lambda targets and externally
 * hosted MCP servers, with their schema reviews — formerly `/v3/tools/gateway`).
 * A gateway's own pages — policies (Cedar editor and test), rate limits, decisions,
 * audit — open hosted from V2 (`?view=gateway…`).
 */
export function V3Governance() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const { current } = useWorkspace();
  const asked = params.get("tab") as Tab | null;
  const tab: Tab = asked && TABS.includes(asked) ? asked : "gateways";
  const [force, setForce] = useState(0);
  const [formRequest, setFormRequest] = useState(0);
  // the first load may come from the backend cache; refresh bypasses it
  const gateways = useLoad(
    () => api.listGovernanceGateways(force > 0),
    `v3-gov-gateways:${current?.id ?? ""}:${force}`,
  );
  // the targets 需要处理 counts — read on the gateways tab only, afresh on each return
  // to it (a review decided on the targets tab clears its item). A member without
  // the targets' read permission simply gets no item.
  const targets = useLoad(
    () => tab !== "gateways"
      ? Promise.resolve(null)
      : Promise.all([
        listLambdaTargets().then((r) => r.targets).catch(() => []),
        listExternalTargets().then((r) => r.targets).catch(() => []),
      ]),
    `v3-gov-targets:${current?.id ?? ""}:${tab}:${force}`,
  );
  const targetAttention = useMemo(
    () => (targets.data ? targetAttentionOf(targets.data[0], targets.data[1], t) : []),
    [targets.data, t],
  );

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.governance.eyebrow")}
        title={t("v3.governance.title")}
        sub={t(`v3.governance.sub.${tab}`)}
        end={
          <>
            <Filters
              value={tab}
              onChange={(next) => setParams(next === "gateways" ? {} : { tab: next })}
              options={[
                { value: "gateways", label: t("v3.governance.tab.gateways") },
                { value: "tools", label: t("v3.governance.tab.tools") },
                { value: "targets", label: t("v3.governance.tab.targets") },
              ]}
            />
            {tab === "targets" && (
              <Btn kind="primary" onClick={() => setFormRequest((n) => n + 1)}><Plus size={14} /> {t("lambdaTool.page.new")}</Btn>
            )}
            {tab === "gateways" && (
              <Btn kind="ghost" title={t("v3.governance.refresh")} disabled={gateways.loading} onClick={() => setForce((n) => n + 1)}>
                <RefreshCw size={14} />
              </Btn>
            )}
          </>
        }
      />
      {tab === "gateways" ? (
        <GatewaysTab data={gateways.data} loading={gateways.loading} error={gateways.error ? gateways.failure : null} targetAttention={targetAttention} />
      ) : tab === "tools" ? <ToolsTab /> : <GatewayToolTargets formRequest={formRequest} />}
    </div>
  );
}

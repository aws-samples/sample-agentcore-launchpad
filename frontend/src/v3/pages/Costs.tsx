import "./costs.css";

import { Bell, BellOff, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { ApiErrorDetail } from "../../components/ApiErrorDetail";
import { ErrorCallout } from "../../components/ErrorCallout";
import { useAuth } from "../../auth/auth-context";
import { type AlertKind, type AlertRuleInfo, api } from "../../lib/api";
import { budgetsApi } from "../../lib/api/budgets";
import { useProject } from "../../workspace/project-context";
import { useWorkspace } from "../../workspace/workspace-context";
import { useFailure } from "../failure";
import { ago } from "../format";
import { useLoad, useToast } from "../hooks";
import { Btn, Chip, Confirm, Dialog, Empty, Filters, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";
import { BillingView } from "./cost-tags/BillingView";
import { DepartmentBudgetView, PlatformBudgetView, ProjectBudgetView } from "./costs/BudgetViews";
import { costViews, pickView } from "./costs/budgetModel";
import {
  COST_PERIODS, type CostPeriod, CUSTOM_MAX_DAYS, customRangeError, DEFAULT_COST_PERIOD, periodLabel, utcToday,
} from "./costs/costPeriod";
import { ModelSpendPanel } from "./costs/ModelSpend";
import { alertDetailText } from "../../lib/alertDetail";
import { Select } from "../select";

/* Spend is labelled an estimate everywhere, and models the price map cannot value
   are named rather than silently counted as free — the same rules as V2. */

const RANGES = ["1h", "6h", "24h", "7d", "30d"] as const;
// error rate and latency read the observability dashboard, which has no 30-day range
const DASHBOARD_RANGES = ["1h", "6h", "24h", "7d"] as const;
const KIND_UNIT: Record<AlertKind, string> = { error_rate: "%", latency_p95_ms: "ms", online_quality: "", cost_mtd_usd: "$" };
const KINDS = Object.keys(KIND_UNIT) as AlertKind[];

/** Money to two decimals (four under a cent, so a bar never sits next to a
 *  misleading $0.00); a null price is "—", never 0. */
function usd(value: number | null | undefined): string {
  if (value == null) return "—";
  return value > 0 && value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}

/** A rule's value in the unit the operator typed it in. */
function shown(kind: AlertKind, value: number | null): string {
  if (value == null) return "—";
  if (kind === "error_rate") return `${(value * 100).toFixed(1)}%`;
  if (kind === "cost_mtd_usd") return `$${value.toFixed(2)}`;
  if (kind === "online_quality") return value.toFixed(2);
  return `${Math.round(value)} ms`;
}

/** Firing is coral; within reach of its threshold (80% of an upper line, 10% above
 *  a lower one) is amber; a rule that could not read its value is off. */
function ruleSignal(rule: AlertRuleInfo): Signal {
  if (!rule.enabled || rule.state === "unknown" || rule.last_value == null) return "off";
  if (rule.state === "firing") return "act";
  const near = rule.comparison === "above" ? rule.last_value >= rule.threshold * 0.8 : rule.last_value <= rule.threshold * 1.1;
  return near ? "wait" : "ok";
}

/** The threshold sits at 75% of the gauge, so headroom and overshoot both read. */
function Gauge({ rule }: { rule: AlertRuleInfo }) {
  const scale = rule.threshold > 0 ? rule.threshold / 0.75 : 1;
  const fill = rule.last_value == null ? 0 : Math.max(0, Math.min(1, rule.last_value / scale));
  return (
    <div className="v3-cost-gauge" data-s={ruleSignal(rule)} aria-hidden="true">
      <i style={{ width: `${fill * 100}%` }} />
      <span className="line" style={{ left: "75%" }} />
    </div>
  );
}

function RuleCard({ rule, onToggle, onRemove }: { rule: AlertRuleInfo; onToggle?: () => void; onRemove?: () => void }) {
  const { t } = useTranslation();
  const s = ruleSignal(rule);
  return (
    <Panel signal={s === "off" ? undefined : s} className="v3-cost-rule">
      <div className="top">
        <Lamp s={s} live={s === "act"} />
        <b title={rule.name || undefined}>{rule.name || t(`v2.alerts.kindName.${rule.kind}`)}</b>
        <Chip s={!rule.enabled ? undefined : rule.state === "firing" ? "act" : rule.state === "ok" ? "ok" : undefined}>
          {rule.enabled ? t(`v2.alerts.state.${rule.state}`) : t("v2.alerts.state.disabled")}
        </Chip>
        {(onToggle || onRemove) && (
          <span className="end">
            {onToggle && (
              <Btn size="sm" kind="ghost" onClick={onToggle} title={t("v2.alerts.toggle")}>
                {rule.enabled ? <BellOff size={13} /> : <Bell size={13} />}
              </Btn>
            )}
            {onRemove && <Btn size="sm" kind="ghost" onClick={onRemove} title={t("v2.alerts.delete")}><Trash2 size={13} /></Btn>}
          </span>
        )}
      </div>
      <div className="cond">
        {t(`v2.alerts.kindName.${rule.kind}`)} {t(`v2.alerts.direction.${rule.comparison}`)} {shown(rule.kind, rule.threshold)}
        {rule.kind !== "cost_mtd_usd" ? ` · ${rule.window}` : ""}
        {rule.has_webhook ? ` · ${t("v3.costs.webhook")}` : ""}
      </div>
      <div className="now">
        <span className="v">{shown(rule.kind, rule.last_value)}</span>
        <small>{rule.last_checked_at ? t("v3.costs.checked", { when: ago(rule.last_checked_at) }) : t("v3.costs.neverChecked")}</small>
      </div>
      <Gauge rule={rule} />
      <div className="detail">{alertDetailText(t, rule.last_detail)}</div>
    </Panel>
  );
}

/* ── spend ───────────────────────────────────────────────────────────────── */

function SpendView() {
  const { t } = useTranslation();
  const toast = useToast();
  const { current } = useWorkspace();
  const ws = current?.id ?? "";
  const [, setParams] = useSearchParams();
  const [range, setRange] = useState<CostPeriod>(DEFAULT_COST_PERIOD);
  // the custom range being typed, and the one applied (only an applied one is queried)
  const [draft, setDraft] = useState(() => ({ start: utcToday(), end: utcToday() }));
  const [custom, setCustom] = useState<{ start: string; end: string } | null>(null);
  const [tick, setTick] = useState(0);
  const waiting = range === "custom" && !custom;
  const report = useLoad(
    () => (waiting ? Promise.resolve(null) : api.costs(range, tick > 0, range === "custom" ? custom ?? undefined : undefined)),
    `v3-costs:${ws}:${range}:${custom?.start ?? ""}:${custom?.end ?? ""}:${tick}`,
  );
  const draftError = customRangeError(draft.start, draft.end);
  const rangeLabel = periodLabel(report.data?.period) || t(`v3.costs.period.${range}`);
  const mtd = useLoad(() => api.costsMonthToDate().catch(() => null), `v3-costs-mtd:${ws}:${tick}`);
  // what the platform watches, as last evaluated — read only, never re-evaluated here
  const rules = useLoad(() => api.alertRules().catch(() => null), `v3-costs-rules:${ws}`);
  const data = report.data;
  const watched = (rules.data?.rules ?? []).filter((r) => ["act", "wait"].includes(ruleSignal(r)));
  const maxAgent = Math.max(...(data?.by_agent ?? []).map((r) => r.est_cost_usd ?? 0), 0) || 1;
  const maxProject = Math.max(...(data?.by_project ?? []).map((r) => r.est_cost_usd), 0) || 1;
  const maxActor = Math.max(...(data?.by_actor ?? []).map((r) => r.est_cost_usd), 0) || 1;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      {rules.data && (
        watched.length > 0 ? (
          <Panel title={t("v3.costs.watchTitle")} signal={watched.some((r) => ruleSignal(r) === "act") ? "act" : "wait"}
            end={<button type="button" className="v3-btn ghost sm" onClick={() => setParams({ view: "alerts" })}>{t("v3.costs.allRules")}</button>}>
            <div className="v3-cost-rules">
              {watched.map((r) => <RuleCard key={r.id} rule={r} />)}
            </div>
          </Panel>
        ) : (
          <Notice s="ok">
            {rules.data.rules.length ? t("v3.costs.allClear", { count: rules.data.rules.length }) : t("v3.costs.noRules")}{" "}
            <button type="button" className="v3-btn ghost sm" onClick={() => setParams({ view: "alerts" })}>{t("v3.costs.allRules")}</button>
          </Notice>
        )
      )}

      <div className="v3-grid c4">
        <Panel signal="info"><Stat label={t("v3.costs.spend", { range: rangeLabel })} value={usd(data?.total_est_cost_usd)} foot={t("v2.costs.estimate")} /></Panel>
        <Panel><Stat label={t("v3.costs.mtd")} value={mtd.data ? usd(mtd.data.est_cost_usd) : "—"} foot={t("v3.costs.mtdFoot")} /></Panel>
        <Panel><Stat label={t("v2.costs.kpiTokens")} value={data ? data.total_tokens.toLocaleString() : "—"} foot={rangeLabel} /></Panel>
        <Panel><Stat label={t("v2.costs.kpiAgents")} value={data?.by_agent.length ?? "—"} /></Panel>
      </div>

      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }} data-testid="v3-costs-period">
        <Filters value={range} onChange={setRange}
          options={COST_PERIODS.map((value) => ({ value, label: t(`v3.costs.period.${value}`) }))} />
        {range === "custom" && (
          <span style={{ display: "inline-flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
            <input type="date" className="v3-input" aria-label={t("v3.costs.from")} value={draft.start}
              max={utcToday()} onChange={(e) => setDraft((d) => ({ ...d, start: e.target.value }))} />
            <span>–</span>
            <input type="date" className="v3-input" aria-label={t("v3.costs.to")} value={draft.end}
              min={draft.start} onChange={(e) => setDraft((d) => ({ ...d, end: e.target.value }))} />
            <Btn size="sm" kind="primary" disabled={draftError !== null}
              disabledReason={draftError ? t(`v3.costs.customError.${draftError}`, { max: CUSTOM_MAX_DAYS }) : undefined}
              onClick={() => setCustom({ ...draft })}>{t("v3.costs.apply")}</Btn>
          </span>
        )}
        <small className="v3-hint">{t("v3.costs.utcNote")}</small>
        <Btn size="sm" kind="ghost" onClick={() => {
          setTick((n) => n + 1);
          toast("ok", t("v2.costs.refreshing"));
        }}><RefreshCw size={13} /> {t("v2.costs.refresh")}</Btn>
        {data && <span style={{ marginLeft: "auto", color: "var(--v3-text-3)", fontSize: 12 }}>{t("v3.costs.generated", { when: ago(data.generated_at) })}</span>}
      </div>

      {data && data.unpriced_models.length > 0 && <Notice s="wait">{t("v2.costs.unpriced", { models: data.unpriced_models.join(", ") })}</Notice>}
      {waiting && <Notice s="info">{t("v3.costs.pickCustom")}</Notice>}

      {data?.by_model && <ModelSpendPanel rows={data.by_model} total={data.total_est_cost_usd} />}

      <div className="v3-grid v3-split" style={{ alignItems: "start" }}>
        <Panel flush title={t("v2.costs.byAgent")}>
          {report.loading && !data ? (
            <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
          ) : report.error ? (
            <div style={{ padding: 20 }}><Notice s="act"><ApiErrorDetail error={report.failure ?? report.error} /></Notice></div>
          ) : !data?.by_agent.length ? (
            <Empty title={t("v2.costs.empty")} />
          ) : (
            <table className="v3-table">
              <thead>
                <tr>
                  <th>{t("v2.costs.colAgent")}</th>
                  <th className="num">{t("v2.costs.colTokens")}</th>
                  <th className="num">{t("v2.costs.colCalls")}</th>
                </tr>
              </thead>
              <tbody>
                {data.by_agent.map((row) => (
                  <tr key={row.service}>
                    <td>
                      <div className="v3-cost-bar">
                        <span style={{ display: "inline-flex", gap: 8, alignItems: "center", minWidth: 0 }}>
                          {row.agent_id ? (
                            <Link to={`/v3/agents?id=${encodeURIComponent(row.agent_id)}`}><b>{row.display_name || row.service}</b></Link>
                          ) : (
                            <b>{row.display_name || row.service}</b>
                          )}
                          {!row.known && <Chip>{t("v2.costs.notInLedger")}</Chip>}
                        </span>
                        <span className="mono">{usd(row.est_cost_usd)}</span>
                        <span className="track"><i className={row.est_cost_usd == null ? "none" : undefined}
                          style={{ width: `${((row.est_cost_usd ?? 0) / maxAgent) * 100}%` }} /></span>
                      </div>
                    </td>
                    <td className="num">{row.tokens.toLocaleString()}</td>
                    <td className="num">{row.llm_calls}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
        {data?.by_project && data.by_project.length > 0 && (
          <Panel flush title={t("v3.costs.byProject")}>
            <table className="v3-table" data-testid="v3-costs-by-project">
              <thead>
                <tr>
                  <th>{t("v3.costs.colProject")}</th>
                  <th className="num">{t("v2.costs.kpiAgents")}</th>
                  <th className="num">{t("v2.costs.colTokens")}</th>
                </tr>
              </thead>
              <tbody>
                {data.by_project.map((row) => (
                  <tr key={row.project_id ?? "—"}>
                    <td>
                      <div className="v3-cost-bar">
                        <span>{row.project_id ? <span className="mono">{row.project_id}</span> : <span style={{ color: "var(--v3-text-3)" }}>{t("v2.costs.unattributed")}</span>}</span>
                        <span className="mono">{usd(row.est_cost_usd)}</span>
                        <span className="track"><i style={{ width: `${(row.est_cost_usd / maxProject) * 100}%` }} /></span>
                      </div>
                    </td>
                    <td className="num">{row.agents}</td>
                    <td className="num">{row.tokens.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Panel>
        )}
        <Panel flush title={t("v2.costs.byActor")}>
          {report.loading && !data ? (
            <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
          ) : !data?.by_actor.length ? (
            <Empty title={t("v2.costs.empty")} />
          ) : (
            <table className="v3-table">
              <thead>
                <tr>
                  <th>{t("v2.costs.colActor")}</th>
                  <th className="num">{t("v2.costs.colSessions")}</th>
                </tr>
              </thead>
              <tbody>
                {data.by_actor.map((row) => (
                  <tr key={row.actor}>
                    <td>
                      <div className="v3-cost-bar">
                        <span>{row.actor === "—" ? <span style={{ color: "var(--v3-text-3)" }}>{t("v2.costs.unattributed")}</span> : row.actor}</span>
                        <span className="mono">{usd(row.est_cost_usd)}</span>
                        <span className="track"><i style={{ width: `${(row.est_cost_usd / maxActor) * 100}%` }} /></span>
                      </div>
                    </td>
                    <td className="num">{row.sessions}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>
    </div>
  );
}

/* ── alerts ──────────────────────────────────────────────────────────────── */

function NewRuleDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const [kind, setKind] = useState<AlertKind>("error_rate");
  const [name, setName] = useState("");
  const [threshold, setThreshold] = useState("");
  const [window, setWindow] = useState("24h");
  const [webhook, setWebhook] = useState("");
  const [busy, setBusy] = useState(false);
  const valid = threshold.trim() !== "" && Number.isFinite(Number(threshold));
  const submit = async () => {
    // typed in percent for a rate, stored as the 0–1 fraction the backend compares
    const raw = Number(threshold);
    setBusy(true);
    try {
      failed.clear();
      await api.createAlertRule({
        kind,
        name: name.trim() || undefined,
        threshold: kind === "error_rate" ? raw / 100 : raw,
        window: kind === "cost_mtd_usd" ? undefined : window,
        webhook_url: webhook.trim() || null,
      });
      toast("ok", t("v2.alerts.created"));
      onDone();
      onClose();
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      wide
      title={t("v2.alerts.newTitle")}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" busy={busy} disabled={!valid} disabledReason={t("story.reason.fillThreshold")} onClick={() => void submit()}>{t("v2.alerts.create")}</Btn>
        </>
      }
    >
      <div style={{ display: "grid", gap: 12 }}>
        <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-costs-rule-error" />
        <label className="v3-field">
          <span>{t("v2.alerts.kind")}</span>
          <Select className="v3-select" value={kind} onChange={(e) => {
            const v = e.target.value as AlertKind;
            setKind(v);
            if (v !== "online_quality" && window === "30d") setWindow("7d");
          }}>
            {KINDS.map((value) => <option key={value} value={value}>{t(`v2.alerts.kindName.${value}`)}</option>)}
          </Select>
          <small className="v3-hint">{t(`v2.alerts.kindHint.${kind}`)}</small>
        </label>
        <label className="v3-field">
          <span>{t("v2.alerts.name")}</span>
          <input className="v3-input" maxLength={64} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <div className="v3-grid c2" style={{ alignItems: "start" }}>
          <label className="v3-field">
            <span>{t("v2.alerts.threshold", { direction: t(`v2.alerts.direction.${kind === "online_quality" ? "below" : "above"}`), unit: KIND_UNIT[kind] || "" })} *</span>
            <input className="v3-input mono" inputMode="decimal" value={threshold} onChange={(e) => setThreshold(e.target.value)} />
          </label>
          {kind !== "cost_mtd_usd" && (
            <label className="v3-field">
              <span>{t("v2.alerts.window")}</span>
              <Select className="v3-select" value={window} onChange={(e) => setWindow(e.target.value)}>
                {(kind === "online_quality" ? RANGES : DASHBOARD_RANGES).map((value) => <option key={value} value={value}>{value}</option>)}
              </Select>
              <small className="v3-hint">{t("v2.alerts.windowHint")}</small>
            </label>
          )}
        </div>
        <label className="v3-field">
          <span>{t("v2.alerts.webhook")}</span>
          <input className="v3-input mono" placeholder="https://…" value={webhook} onChange={(e) => setWebhook(e.target.value)} />
          <small className="v3-hint">{t("v2.alerts.webhookHint")}</small>
        </label>
      </div>
    </Dialog>
  );
}

function AlertsView() {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const { current } = useWorkspace();
  const [tick, setTick] = useState(0);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState<AlertRuleInfo | null>(null);
  const list = useLoad(() => api.alertRules(), `v3-alerts:${current?.id ?? ""}:${tick}`);
  const rows = list.data?.rules ?? [];
  const refresh = () => setTick((n) => n + 1);

  const recheck = async () => {
    setBusy(true);
    try {
      failed.clear();
      // notify=false: opening or refreshing the page must never page anyone
      const result = await api.evaluateAlerts(false);
      toast("ok", t("v2.alerts.rechecked", { firing: result.firing }));
      refresh();
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
    }
  };
  const toggle = async (rule: AlertRuleInfo) => {
    try {
      failed.clear();
      await api.updateAlertRule(rule.id, { enabled: !rule.enabled });
      refresh();
    } catch (err) {
      failed.fail(err);
    }
  };
  const remove = async (rule: AlertRuleInfo) => {
    try {
      failed.clear();
      await api.deleteAlertRule(rule.id);
      toast("ok", t("v2.alerts.deleted"));
      refresh();
    } catch (err) {
      failed.fail(err);
    }
  };

  const firing = rows.filter((r) => r.state === "firing" && r.enabled).length;
  const near = rows.filter((r) => ruleSignal(r) === "wait").length;
  const unknown = rows.filter((r) => r.state === "unknown" && r.enabled).length;
  // firing first, then near, then the rest
  const order: Record<Signal, number> = { act: 0, wait: 1, ok: 2, info: 3, off: 4 };
  const sorted = [...rows].sort((a, b) => order[ruleSignal(a)] - order[ruleSignal(b)]);

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-costs-alerts-error" />
      <div className="v3-grid c4">
        <Panel signal={firing ? "act" : undefined}><Stat label={t("v2.alerts.kpiFiring")} value={list.data ? firing : "—"} signal={firing ? "act" : undefined} /></Panel>
        <Panel signal={near ? "wait" : undefined}><Stat label={t("v3.costs.near")} value={list.data ? near : "—"} foot={t("v3.costs.nearFoot")} /></Panel>
        <Panel><Stat label={t("v2.alerts.kpiUnknown")} value={list.data ? unknown : "—"} foot={t("v2.alerts.unknownSub")} /></Panel>
        <Panel><Stat label={t("v2.alerts.kpiTotal")} value={list.data ? rows.length : "—"} /></Panel>
      </div>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <span style={{ color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.alerts.listSub")}</span>
        <span style={{ marginLeft: "auto", display: "inline-flex", gap: 8 }}>
          <Btn size="sm" disabled={busy} onClick={() => void recheck()}><RefreshCw size={13} /> {t("v2.alerts.recheck")}</Btn>
          <Btn size="sm" kind="primary" onClick={() => setAdding(true)}><Plus size={13} /> {t("v2.alerts.new")}</Btn>
        </span>
      </div>
      {list.loading && !list.data ? (
        <Panel><Skeleton rows={4} /></Panel>
      ) : list.error ? (
        <Notice s="act"><ApiErrorDetail error={list.failure ?? list.error} /></Notice>
      ) : rows.length === 0 ? (
        <Panel><Empty title={t("v2.alerts.empty")} /></Panel>
      ) : (
        <div className="v3-cost-rules">
          {sorted.map((r) => <RuleCard key={r.id} rule={r} onToggle={() => void toggle(r)} onRemove={() => setRemoving(r)} />)}
        </div>
      )}
      {adding && <NewRuleDialog onClose={() => setAdding(false)} onDone={refresh} />}
      {removing && (
        <Confirm
          title={t("v3.costs.deleteTitle")}
          confirmLabel={t("v2.alerts.delete")}
          cancelLabel={t("v3.common.cancel")}
          danger
          onCancel={() => setRemoving(null)}
          onConfirm={() => {
            const target = removing;
            setRemoving(null);
            void remove(target);
          }}
        >
          {t("v3.costs.deleteBody", { name: removing.name || t(`v2.alerts.kindName.${removing.kind}`) })}
        </Confirm>
      )}
    </div>
  );
}

/**
 * Where the money went, and what the platform watches — one page, `?view=` states as
 * in V2: the workspace spend report (bare) and its rules (`alerts`), administrators';
 * then the budget dashboards (projects §11.3), each offered only to whoever can open
 * it — the current project's budget (`project`, every member), a department's
 * (`department&id=`, its administrators and the platform's) and the platform's
 * (`platform`, administrators). The spend view leads with any rule that is firing or
 * close to its line, so the threshold is the headline.
 */
export function V3Costs() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const project = useProject();
  const [params, setParams] = useSearchParams();
  const me = useLoad(() => budgetsApi.me().catch(() => null), "v3-costs-budgets-me");
  const views = costViews(isAdmin, me.data, project.current !== null);
  const view = pickView(params.get("view") ?? (isAdmin ? "spend" : "project"), views);
  // the spend view's basis (cost-tags §1.4-2): the token estimate, or what AWS billed
  const basis = params.get("basis") === "billing" ? "billing" : "estimate";
  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.costs.eyebrow")}
        title={t("v2.costs.title")}
        sub={t("v2.costs.desc")}
        end={
          <>
            {view === "spend" && (
              <Filters
                value={basis}
                onChange={(next) => setParams(next === "billing" ? { basis: next } : {})}
                options={(["estimate", "billing"] as const).map((value) => ({ value, label: t(`v3.costTags.basis.${value}`) }))}
              />
            )}
            {view && views.length > 1 && (
              <Filters
                value={view}
                onChange={(next) => setParams(next === "spend" ? {} : { view: next })}
                options={views.map((value) => ({ value, label: t(`v3.costs.view.${value}`) }))}
              />
            )}
          </>
        }
      />
      {view === "spend" ? (basis === "billing" ? <BillingView /> : <SpendView />)
        : view === "alerts" ? <AlertsView />
        : view === "project" && project.current ? <ProjectBudgetView projectId={project.current.id}
            manageInDepartment={me.data?.budget_scope === "department"} />
        : view === "department" ? <DepartmentBudgetView departments={me.data?.departments ?? []} me={me.data} />
        : view === "platform" ? <PlatformBudgetView />
        : me.loading ? <Panel><Skeleton rows={3} /></Panel>
        : <Panel><Empty title={t("v3.costs.budget.nothing")} /></Panel>}
    </div>
  );
}

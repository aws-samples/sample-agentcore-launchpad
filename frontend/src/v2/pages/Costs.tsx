import { RefreshCw } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import {
  type AlertKind,
  type AlertRuleInfo,
  api,
  type CostActorRow,
  type CostRange,
  type CostRow,
  errorMessage,
} from "../../lib/api";
import { creatorLabel, fmtNumber, fmtTime } from "../format";
import { useLoad, useV2Toast } from "../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  Field,
  FilterSelect,
  Kpi,
  Lamp,
  LinkButton,
  Modal,
  PageHeader,
  Select,
  SubTabs,
  Table,
  Tag,
} from "../ui";
import { SIGNAL_TONE } from "../signals";
import {
  ALERT_KINDS,
  coerceWindow,
  COST_RANGES,
  createPayload,
  draftFromRule,
  draftValid,
  emptyDraft,
  fixedWindowKey,
  fmtRuleValue,
  fmtUsd,
  NATURAL_COMPARISON,
  patchPayload,
  type RuleDraft,
  ruleSignal,
  ruleStateKey,
  THRESHOLD_UNIT,
  totalSessions,
  windowOptions,
} from "./costs/alertRules";

type CostsTab = "costs" | "alerts";

function asRange(value: string | null): CostRange {
  return COST_RANGES.includes(value as CostRange) ? (value as CostRange) : "24h";
}

/* ---------- spend ---------- */

function CostsView({ range, onRange }: { range: CostRange; onRange: (r: CostRange) => void }) {
  const { t } = useTranslation();
  const [nonce, setNonce] = useState(0);
  // set by the refresh button; consumed by the next fetch only, so a later range
  // change reads the 60 s cache again instead of re-scanning
  const forceReport = useRef(false);
  const forceMtd = useRef(false);
  const report = useLoad(() => {
    const force = forceReport.current;
    forceReport.current = false;
    return api.costs(range, force);
  }, `v2-costs:${range}:${nonce}`);
  const mtd = useLoad(() => {
    const force = forceMtd.current;
    forceMtd.current = false;
    return api.costsMonthToDate(force);
  }, `v2-costs-mtd:${nonce}`);
  const data = report.data;
  const refresh = () => {
    forceReport.current = true;
    forceMtd.current = true;
    setNonce((n) => n + 1);
  };

  const byAgent = [...(data?.by_agent ?? [])].sort(
    (a, b) => (b.est_cost_usd ?? -1) - (a.est_cost_usd ?? -1),
  );
  const byActor = data?.by_actor ?? [];
  const sessions = totalSessions(byActor);
  const unpriced = [...new Set([...(data?.unpriced_models ?? []), ...(mtd.data?.unpriced_models ?? [])])].sort();

  const agentColumns: Column<CostRow>[] = [
    {
      key: "agent",
      title: t("v2.costs.colAgent"),
      render: (row) =>
        row.known && row.agent_id ? (
          <>
            <Link className="v2-link" to={`/v2/agents?view=detail&id=${encodeURIComponent(row.agent_id)}`}>
              {row.display_name || row.agent_name || row.service}
            </Link>
            {(row.display_name || row.agent_name) && <span className="sub mono">{row.service}</span>}
          </>
        ) : (
          <span className="v2-row" style={{ gap: 6 }}>
            <span className="mono">{row.service}</span>
            <Tag tone="gray">{t("v2.costs.notInConsole")}</Tag>
          </span>
        ),
    },
    { key: "sessions", title: t("v2.costs.colSessions"), className: "nowrap", render: (row) => fmtNumber(row.sessions) },
    { key: "tokens", title: t("v2.costs.colTokens"), className: "nowrap", render: (row) => fmtNumber(row.tokens) },
    { key: "calls", title: t("v2.costs.colCalls"), className: "nowrap", render: (row) => fmtNumber(row.llm_calls) },
    { key: "cost", title: t("v2.costs.colCost"), className: "nowrap", render: (row) => fmtUsd(row.est_cost_usd) },
  ];

  const actorColumns: Column<CostActorRow>[] = [
    {
      key: "actor",
      title: t("v2.costs.colActor"),
      render: (row) =>
        row.attributed ? row.actor : <span className="v2-muted">{t("v2.costs.unattributed")}</span>,
    },
    { key: "sessions", title: t("v2.costs.colSessions"), className: "nowrap", render: (row) => fmtNumber(row.sessions) },
    { key: "tokens", title: t("v2.costs.colTokens"), className: "nowrap", render: (row) => fmtNumber(row.tokens) },
    { key: "cost", title: t("v2.costs.colCost"), className: "nowrap", render: (row) => fmtUsd(row.est_cost_usd) },
  ];

  return (
    <>
      <div className="v2-toolbar">
        <FilterSelect
          label={t("v2.common.timeRange")}
          value={range}
          onChange={(v) => onRange(v as CostRange)}
          options={COST_RANGES.map((r) => ({ value: r, label: t(`v2.costs.range.${r}`) }))}
          testId="v2-costs-range"
        />
        <Button onClick={refresh} disabled={report.loading} testId="v2-costs-refresh">
          <RefreshCw size={14} aria-hidden="true" />
          {t("v2.common.refresh")}
        </Button>
        <span className="end">
          {data?.cache?.hit && (
            <span className="v2-count">{t("v2.observability.cachedAgo", { s: Math.round(data.cache.age_seconds) })}</span>
          )}
          {data && <span className="v2-muted">{t("v2.costs.generatedAt", { time: fmtTime(data.generated_at) })}</span>}
        </span>
      </div>
      <Alert tone="info">
        <span data-testid="v2-costs-note">{t("v2.costs.note")}</span>
      </Alert>
      {report.error && (
        <Alert tone="error" action={<LinkButton onClick={report.reload}>{t("v2.common.retry")}</LinkButton>}>
          <span data-testid="v2-costs-error">{report.error}</span>
        </Alert>
      )}
      {unpriced.length > 0 && (
        <Alert tone="warn">
          <span data-testid="v2-costs-unpriced">{t("v2.costs.unpriced", { models: unpriced.join(", ") })}</span>
        </Alert>
      )}
      <div className="v2-kpis">
        <Kpi
          label={t("v2.costs.kpiMtd")}
          value={fmtUsd(mtd.data?.est_cost_usd)}
          sub={mtd.error ? t("v2.costs.mtdError") : mtd.data ? t("v2.costs.mtdSub", { hours: mtd.data.hours }) : t("v2.common.loading")}
          testId="v2-costs-kpi-mtd"
        />
        <Kpi
          label={t("v2.costs.kpiTotal")}
          value={fmtUsd(data?.total_est_cost_usd)}
          sub={t(`v2.costs.range.${range}`)}
          testId="v2-costs-kpi-total"
        />
        <Kpi label={t("v2.costs.kpiTokens")} value={data ? fmtNumber(data.total_tokens) : "—"} testId="v2-costs-kpi-tokens" />
        <Kpi
          label={t("v2.costs.kpiSessions")}
          value={data ? `${fmtNumber(sessions.count)}${sessions.capped ? "+" : ""}` : "—"}
          sub={t("v2.costs.kpiSessionsSub")}
          testId="v2-costs-kpi-sessions"
        />
      </div>
      <Card title={t("v2.costs.byAgent")} sub={t("v2.costs.byAgentSub")} flush testId="v2-costs-by-agent">
        <Table
          columns={agentColumns}
          rows={byAgent}
          rowKey={(row) => row.service}
          loading={report.loading}
          error={report.error}
          onRetry={report.reload}
          empty={t("v2.costs.empty")}
          testId="v2-costs-agents"
        />
      </Card>
      <Card title={t("v2.costs.byActor")} sub={t("v2.costs.byActorSub")} flush testId="v2-costs-by-actor">
        <Table
          columns={actorColumns}
          rows={byActor}
          rowKey={(row) => row.actor}
          loading={report.loading}
          error={report.error}
          onRetry={report.reload}
          empty={t("v2.costs.empty")}
          testId="v2-costs-actors"
        />
      </Card>
    </>
  );
}

/* ---------- alert rules ---------- */

function RuleModal({
  editing,
  onClose,
  onSaved,
}: {
  /** null: a new rule */
  editing: AlertRuleInfo | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [draft, setDraft] = useState<RuleDraft>(() => (editing ? draftFromRule(editing) : emptyDraft()));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (patch: Partial<RuleDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const kind = draft.kind;
  const windows = windowOptions(kind);
  const fixed = fixedWindowKey(kind);
  const valid = draftValid(draft, editing);
  const showUrl = !editing?.has_webhook || draft.webhookMode === "replace";

  const submit = async () => {
    if (busy || !valid) return;
    setBusy(true);
    setError(null);
    try {
      if (editing) {
        const patch = patchPayload(editing, draft);
        if (Object.keys(patch).length) await api.updateAlertRule(editing.id, patch);
        toast("success", t("v2.alerts.saved"));
      } else {
        await api.createAlertRule(createPayload(draft));
        toast("success", t("v2.alerts.created"));
      }
      onSaved();
      onClose();
    } catch (err) {
      // the server's SSRF / validation refusal is shown in the dialog, next to the input
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={t(editing ? "v2.alerts.editTitle" : "v2.alerts.newTitle")}
      open
      onClose={onClose}
      testId="v2-alert-modal"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={busy || !valid} onClick={() => void submit()} testId="v2-alert-submit">
            {t(editing ? "v2.common.save" : "v2.common.create")}
          </Button>
        </>
      }
    >
      <div className="v2-form">
        {error && (
          <div data-testid="v2-alert-error">
            <Alert tone="error">{error}</Alert>
          </div>
        )}
        <Field label={t("v2.alerts.kind")} hint={t(`v2.alerts.kindHint.${kind}`)}>
          {editing ? (
            // the backend does not change a rule's kind; delete and re-create instead
            <span data-testid="v2-alert-kind-fixed">{t(`v2.alerts.kindName.${kind}`)}</span>
          ) : (
            <Select
              value={kind}
              options={ALERT_KINDS.map((value) => ({ value, label: t(`v2.alerts.kindName.${value}`) }))}
              onChange={(v) => {
                const next = v as AlertKind;
                set({ kind: next, threshold: "", window: coerceWindow(next, draft.window) });
              }}
              ariaLabel={t("v2.alerts.kind")}
              testId="v2-alert-kind"
            />
          )}
        </Field>
        <Field label={t("v2.alerts.name")} hint={t("v2.alerts.nameHint")}>
          <input
            className="v2-input"
            maxLength={64}
            value={draft.name}
            placeholder={t(`v2.alerts.kindName.${kind}`)}
            onChange={(e) => set({ name: e.target.value })}
            data-testid="v2-alert-name"
          />
        </Field>
        <Field
          label={t("v2.alerts.threshold", {
            direction: t(`v2.alerts.direction.${NATURAL_COMPARISON[kind]}`),
            unit: THRESHOLD_UNIT[kind],
          })}
          hint={t("v2.alerts.thresholdHint")}
          required
        >
          <input
            className="v2-input"
            inputMode="decimal"
            value={draft.threshold}
            onChange={(e) => set({ threshold: e.target.value })}
            onKeyDown={(e) => e.key === "Enter" && void submit()}
            data-testid="v2-alert-threshold"
          />
        </Field>
        {windows.length > 0 ? (
          <Field label={t("v2.alerts.window")} hint={t("v2.alerts.windowHint")}>
            <Select
              value={draft.window}
              options={windows.map((value) => ({ value, label: t(`v2.costs.range.${value}`) }))}
              onChange={(v) => set({ window: v as RuleDraft["window"] })}
              ariaLabel={t("v2.alerts.window")}
              testId="v2-alert-window"
            />
          </Field>
        ) : (
          fixed && (
            <Field label={t("v2.alerts.window")}>
              <span className="v2-muted">{t(fixed)}</span>
            </Field>
          )
        )}
        <Field label={t("v2.alerts.webhook")} hint={t("v2.alerts.webhookHint")}>
          {editing?.has_webhook && (
            <Select
              value={draft.webhookMode}
              options={(["keep", "replace", "remove"] as const).map((value) => ({
                value,
                label: t(`v2.alerts.webhookMode.${value}`),
              }))}
              onChange={(v) => set({ webhookMode: v as RuleDraft["webhookMode"], webhook: "" })}
              ariaLabel={t("v2.alerts.webhook")}
              testId="v2-alert-webhook-mode"
            />
          )}
          {showUrl && (
            <input
              className="v2-input mono"
              placeholder="https://…"
              maxLength={512}
              value={draft.webhook}
              onChange={(e) => set({ webhook: e.target.value })}
              data-testid="v2-alert-webhook"
            />
          )}
        </Field>
      </div>
    </Modal>
  );
}

function SchedulerBanner() {
  const { t } = useTranslation();
  const info = useLoad(() => api.alertScheduler(), "v2-alerts-scheduler");
  if (!info.data) return null;
  return (
    <div data-testid={info.data.enabled ? "v2-alerts-scheduler-on" : "v2-alerts-scheduler-off"}>
      {info.data.enabled ? (
        <Alert tone="info">
          {t("v2.alerts.schedulerOn", { minutes: Math.max(1, Math.round(info.data.interval_seconds / 60)) })}
        </Alert>
      ) : (
        <Alert tone="warn">{t("v2.alerts.schedulerOff")}</Alert>
      )}
    </div>
  );
}

function AlertsView() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const list = useLoad(() => api.alertRules(), "v2-alert-rules");
  const rows = list.data?.rules ?? [];
  const [checking, setChecking] = useState(false);
  const [modal, setModal] = useState<{ editing: AlertRuleInfo | null } | null>(null);
  const [toDelete, setToDelete] = useState<AlertRuleInfo | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const recheck = async () => {
    setChecking(true);
    try {
      // never notifies: only the background check on this server posts to webhooks
      const result = await api.evaluateAlerts();
      toast("success", t("v2.alerts.rechecked", { firing: result.firing, unknown: result.unknown }));
      list.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setChecking(false);
    }
  };

  const toggle = async (rule: AlertRuleInfo) => {
    setBusyId(rule.id);
    try {
      await api.updateAlertRule(rule.id, { enabled: !rule.enabled });
      toast("success", t(rule.enabled ? "v2.alerts.disabled" : "v2.alerts.enabled"));
      list.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async () => {
    if (!toDelete) return;
    setBusyId(toDelete.id);
    try {
      await api.deleteAlertRule(toDelete.id);
      toast("success", t("v2.alerts.deleted"));
      setToDelete(null);
      list.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusyId(null);
    }
  };

  const columns: Column<AlertRuleInfo>[] = [
    {
      key: "state",
      title: t("v2.alerts.colState"),
      className: "nowrap",
      render: (row) => {
        const signal = ruleSignal(row);
        return (
          <span className="v2-row" style={{ gap: 6 }} data-testid={`v2-alert-state-${row.id}`}>
            <Lamp signal={signal} />
            <Tag tone={SIGNAL_TONE[signal]}>{t(ruleStateKey(row))}</Tag>
          </span>
        );
      },
    },
    {
      key: "rule",
      title: t("v2.alerts.colRule"),
      render: (row) => (
        <>
          <span>{row.name || t(`v2.alerts.kindName.${row.kind}`)}</span>
          {row.name && <span className="sub">{t(`v2.alerts.kindName.${row.kind}`)}</span>}
        </>
      ),
    },
    {
      key: "condition",
      title: t("v2.alerts.colCondition"),
      render: (row) => {
        const fixed = fixedWindowKey(row.kind);
        return (
          <>
            <span className="nowrap">
              {t(`v2.alerts.direction.${row.comparison}`)} {fmtRuleValue(row.kind, row.threshold)}
            </span>
            <span className="sub">{fixed ? t(fixed) : t(`v2.costs.range.${row.window}`)}</span>
          </>
        );
      },
    },
    {
      key: "value",
      title: t("v2.alerts.colValue"),
      render: (row) => (
        <>
          <span>{fmtRuleValue(row.kind, row.last_value)}</span>
          {row.last_detail && <span className="sub">{row.last_detail}</span>}
        </>
      ),
    },
    {
      key: "checked",
      title: t("v2.alerts.colChecked"),
      className: "nowrap",
      render: (row) => (
        <>
          <span>{fmtTime(row.last_checked_at)}</span>
          {row.last_fired_at && <span className="sub">{t("v2.alerts.lastFired", { time: fmtTime(row.last_fired_at) })}</span>}
        </>
      ),
    },
    {
      key: "webhook",
      title: t("v2.alerts.colWebhook"),
      className: "nowrap",
      render: (row) => (
        <>
          {row.has_webhook ? <Tag tone="blue">{t("v2.common.yes")}</Tag> : <span className="v2-muted">{t("v2.common.no")}</span>}
          {row.last_notified_at && <span className="sub">{t("v2.alerts.lastNotified", { time: fmtTime(row.last_notified_at) })}</span>}
        </>
      ),
    },
    { key: "creator", title: t("v2.common.creator"), className: "nowrap", render: (row) => creatorLabel(row) },
    {
      key: "actions",
      title: t("v2.common.actions"),
      className: "nowrap",
      render: (row) => (
        <span className="v2-row" style={{ gap: 12 }}>
          <LinkButton onClick={() => setModal({ editing: row })} testId={`v2-alert-edit-${row.id}`}>
            {t("v2.common.edit")}
          </LinkButton>
          <LinkButton disabled={busyId === row.id} onClick={() => void toggle(row)} testId={`v2-alert-toggle-${row.id}`}>
            {t(row.enabled ? "v2.alerts.disable" : "v2.alerts.enable")}
          </LinkButton>
          <LinkButton danger disabled={busyId === row.id} onClick={() => setToDelete(row)} testId={`v2-alert-delete-${row.id}`}>
            {t("v2.common.delete")}
          </LinkButton>
        </span>
      ),
    },
  ];

  const enabled = rows.filter((row) => row.enabled);
  const firing = enabled.filter((row) => row.state === "firing").length;
  const unknown = enabled.filter((row) => row.state === "unknown").length;

  return (
    <>
      <SchedulerBanner />
      <div className="v2-kpis">
        <Kpi label={t("v2.alerts.kpiFiring")} value={list.data ? firing : "—"} tone={firing ? "bad" : undefined} testId="v2-alerts-kpi-firing" />
        <Kpi label={t("v2.alerts.kpiUnknown")} value={list.data ? unknown : "—"} sub={t("v2.alerts.unknownSub")} />
        <Kpi label={t("v2.alerts.kpiTotal")} value={list.data ? rows.length : "—"} />
      </div>
      <Card
        title={t("v2.alerts.listTitle")}
        sub={t("v2.alerts.listSub")}
        flush
        testId="v2-alerts"
        end={
          <span className="v2-row">
            <Button size="sm" disabled={checking || rows.length === 0} onClick={() => void recheck()} testId="v2-alerts-recheck">
              <RefreshCw size={13} aria-hidden="true" />
              {checking ? t("v2.alerts.rechecking") : t("v2.alerts.recheck")}
            </Button>
            <Button size="sm" kind="primary" onClick={() => setModal({ editing: null })} testId="v2-alerts-new">
              {t("v2.alerts.new")}
            </Button>
          </span>
        }
      >
        <Table
          columns={columns}
          rows={rows}
          rowKey={(row) => row.id}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          empty={t("v2.alerts.empty")}
          testId="v2-alerts-table"
        />
      </Card>
      {modal && (
        <RuleModal
          key={modal.editing?.id ?? "new"}
          editing={modal.editing}
          onClose={() => setModal(null)}
          onSaved={list.reload}
        />
      )}
      <Confirm
        open={toDelete !== null}
        title={t("v2.alerts.confirmDeleteTitle")}
        body={t("v2.alerts.confirmDeleteBody", {
          name: toDelete ? toDelete.name || t(`v2.alerts.kindName.${toDelete.kind}`) : "",
        })}
        confirmLabel={t("v2.common.delete")}
        danger
        busy={busyId !== null}
        onConfirm={() => void remove()}
        onClose={() => setToDelete(null)}
      />
    </>
  );
}

/**
 * 成本与告警 — admin-only: what this workspace's agents cost (by agent and by person,
 * for a range and for the month to date; estimates from span tokens × the price map),
 * and threshold alerts on error rate, p95 latency, online quality and spend.
 * `?view=costs` (default) | `?view=alerts`; `range=` keeps the spend window.
 */
export function V2Costs() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const [params, setParams] = useSearchParams();
  const view: CostsTab = params.get("view") === "alerts" ? "alerts" : "costs";
  const range = asRange(params.get("range"));

  // Members never fire the admin page's requests (every route is ADMIN).
  if (!isAdmin) {
    return (
      <>
        <PageHeader title={t("v2.costs.title")} />
        <Card testId="v2-costs-forbidden">
          <Alert tone="warn">{t("v2.costs.forbiddenBody")}</Alert>
        </Card>
      </>
    );
  }

  const go = (next: { view?: CostsTab; range?: CostRange }) => {
    const p: Record<string, string> = {};
    const v = next.view ?? view;
    if (v !== "costs") p.view = v;
    const r = next.range ?? range;
    if (r !== "24h") p.range = r;
    setParams(p);
  };

  return (
    <>
      <PageHeader
        title={t("v2.costs.title")}
        desc={t("v2.costs.desc")}
        tabs={
          <SubTabs
            value={view}
            onChange={(next) => go({ view: next })}
            tabs={(["costs", "alerts"] as const).map((value) => ({ value, label: t(`v2.costs.tab.${value}`) }))}
          />
        }
      />
      {view === "costs" ? <CostsView range={range} onRange={(r) => go({ range: r })} /> : <AlertsView />}
    </>
  );
}

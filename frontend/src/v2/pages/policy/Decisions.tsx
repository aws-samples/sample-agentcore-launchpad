import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  api,
  type Policy,
  type PolicyDecision,
  type PolicyDecisionOutcome,
  type PolicyDecisionSource,
  type PolicyEngine,
  type PolicyEntities,
  type V2Range,
} from "../../../lib/api";
import { governanceError } from "../../../lib/governance";
import { decisionPolicyDeny } from "../../../lib/policy-deny";
import { fmtTime, RANGE_HOURS, RANGES, rangeLabel } from "../../format";
import { useLoad, usePaged } from "../../hooks";
import { Alert, Button, Card, type Column, Descriptions, Drawer, FilterSelect, Kpi, LinkButton, Pager, Table, Tag } from "../../ui";
import { PolicyDenyCard } from "../chat/PolicyDenyCard";
import { classicObservabilityToV2 } from "../observability/classicUrl";
import { gatewayPolicyHref } from "./common";
import { DecisionTag, PolicyModeTag } from "./widgets";

// Global parity (amendment 9): rows are written by the Gateway interceptor and the simulator;
// `hub` (the retired invoke gate) and `runtime` (the retired runtime guard) are HISTORIC —
// kept readable, greyed, offered as filters only while such rows are on the timeline
const SOURCES: PolicyDecisionSource[] = ["gateway", "simulate"];
const HISTORIC_SOURCES: PolicyDecisionSource[] = ["hub", "runtime"];
const isHistoric = (source: PolicyDecisionSource) => HISTORIC_SOURCES.includes(source);
const OUTCOMES: PolicyDecisionOutcome[] = ["allow", "deny", "would_deny", "error"];
const LIMIT = 500;

/** `since` for a range: now minus the range, ISO. */
function sinceFor(range: V2Range): string {
  return new Date(Date.now() - RANGE_HOURS[range] * 3_600_000).toISOString();
}

/**
 * 决策 of one engine over a window — one timeline over `GET /api/policy-engines/{id}/decisions`:
 * Gateway interceptor rows (every tool call decided for the user the agent acts for, per
 * gateway) and recorded simulator dry runs. Observability only: nothing here changes a
 * decision. Historic `hub` / `runtime` rows written before the Global-parity change (the
 * retired invoke gate and runtime guard) stay readable and render greyed with a "historic"
 * mark. Unlike Global's span-derived view the principal is present on every row.
 */
export function DecisionsTab({ engine, entities, policies = null }: { engine: PolicyEngine; entities: PolicyEntities | null; policies?: Policy[] | null }) {
  const { t } = useTranslation();
  const [range, setRange] = useState<V2Range>("24h");
  const [gatewayId, setGatewayId] = useState("");
  const [detail, setDetail] = useState<PolicyDecision | null>(null);
  // the workspace's policy list names the ids a row carries (the card names the policy)
  const names = useMemo(() => Object.fromEntries((policies ?? []).map((p) => [p.id, p.name])), [policies]);
  const [source, setSource] = useState("");
  const [decision, setDecision] = useState("");
  const [agentId, setAgentId] = useState("");
  const [principal, setPrincipal] = useState("");
  const [nonce, setNonce] = useState(0);
  // the window is anchored when the filters change, not on every render
  const since = useMemo(() => sinceFor(range), [range, nonce]); // eslint-disable-line react-hooks/exhaustive-deps
  const { data, loading, error, reload } = useLoad(
    () =>
      api
        .policyDecisions(engine.id, {
          gateway_id: gatewayId || undefined,
          source: source as PolicyDecisionSource | "",
          decision: decision as PolicyDecisionOutcome | "",
          agent_id: agentId || undefined,
          principal: principal || undefined,
          since,
          limit: LIMIT,
        })
        .catch((err: unknown) => Promise.reject(new Error(governanceError(err)))),
    `policy-decisions:${engine.id}:${gatewayId}:${since}:${source}:${decision}:${agentId}:${principal}:${nonce}`,
  );
  const rows = useMemo(() => data?.decisions ?? [], [data]);
  const paged = usePaged(rows, 15);
  const totals = useMemo(
    () => ({
      allow: rows.filter((d) => d.decision === "allow").length,
      deny: rows.filter((d) => d.decision === "deny").length,
      // N17-obs508: gateway allows a log_only policy would have denied count as evidence too
      wouldDeny: rows.filter((d) => d.decision === "would_deny" || d.log_only_would).length,
      error: rows.filter((d) => d.decision === "error").length,
    }),
    [rows],
  );
  const agentName = (id: string | null) => entities?.agents.find((a) => a.id === id)?.name ?? id ?? "—";
  // the Gateway interceptor's columns appear once such rows exist (or the filter asks for them)
  const gateway = source === "gateway" || rows.some((d) => d.source === "gateway");
  // historic sources are offered as filters only while such rows are on the timeline
  const historic = HISTORIC_SOURCES.filter((h) => source === h || rows.some((d) => d.source === h));
  const sources: PolicyDecisionSource[] = [...SOURCES, ...historic];
  const gatewayName = (id: string | null | undefined, name?: string | null) =>
    name ?? engine.attachments.find((a) => a.gateway_id === id)?.gateway_name ?? id ?? "—";
  const gatewayColumns: Column<PolicyDecision>[] = [
    {
      key: "gateway",
      title: t("v2.policy.dec.colGateway"),
      className: "v2-decision-identity",
      render: (d) =>
        d.gateway_id ? (
          <Link className="v2-governance-link" to={gatewayPolicyHref(d.gateway_id)} data-testid={`v2-decision-gateway-${d.id}`} title={d.gateway_id}>
            {gatewayName(d.gateway_id, d.gateway_name)}
          </Link>
        ) : (
          <span className="v2-muted">—</span>
        ),
    },
    {
      key: "stage",
      title: t("v2.governance.policy.dec.colStage"),
      render: (d) =>
        d.source === "gateway" ? (
          <span data-testid={`v2-decision-stage-${d.id}`}>
            <Tag tone="outline">{t(`v2.governance.policy.dec.stage.${d.stage ?? "final"}`, { defaultValue: d.stage ?? "—" })}</Tag>
            {d.event_seq != null && <span className="sub mono">#{d.event_seq}</span>}
          </span>
        ) : (
          <span className="v2-muted">—</span>
        ),
    },
    {
      key: "psid",
      title: t("v2.governance.policy.dec.colPsid"),
      render: (d) =>
        d.source === "gateway" && d.session_psid ? (
          <span className="mono" title={d.session_psid} data-testid={`v2-decision-psid-${d.id}`}>
            {d.session_psid.slice(0, 8)}
          </span>
        ) : (
          <span className="v2-muted">—</span>
        ),
    },
    {
      key: "inputKeys",
      title: t("v2.governance.policy.dec.colInputKeys"),
      className: "v2-decision-inputs",
      render: (d) =>
        d.source === "gateway" && (d.input_keys ?? []).length > 0 ? (
          <div className="v2-tags" data-testid={`v2-decision-input-keys-${d.id}`}>
            {(d.input_keys ?? []).map((k) => (
              <Tag key={k} tone="gray">
                <span className="mono">{k}</span>
              </Tag>
            ))}
          </div>
        ) : (
          <span className="v2-muted">—</span>
        ),
    },
  ];

  // the gateways this engine is attached to, plus any other gateway the rows name
  const gatewayFilterOptions = useMemo(() => {
    const seen = new Map<string, string>();
    for (const a of engine.attachments) seen.set(a.gateway_id, a.gateway_name ?? a.gateway_id);
    for (const d of rows) if (d.gateway_id && !seen.has(d.gateway_id)) seen.set(d.gateway_id, d.gateway_name ?? d.gateway_id);
    if (gatewayId && !seen.has(gatewayId)) seen.set(gatewayId, gatewayId);
    return [...seen].map(([value, label]) => ({ value, label }));
  }, [engine.attachments, gatewayId, rows]);

  const columns: Column<PolicyDecision>[] = [
    {
      key: "time",
      title: t("v2.governance.policy.dec.colTime"),
      className: "nowrap",
      render: (d) => (
        <>
          {fmtTime(d.created_at)}
          <span className={isHistoric(d.source) ? "sub mono v2-muted" : "sub mono"}>
            {d.source} · {d.channel}
          </span>
          {isHistoric(d.source) && (
            <Tag tone="gray" title={t("v2.governance.policy.dec.historicHint")} testId={`v2-decision-historic-${d.id}`}>
              {t("v2.governance.policy.dec.historic")}
            </Tag>
          )}
          {d.source === "gateway" && (
            <span className="sub mono" data-testid={`v2-decision-ingested-${d.id}`}>
              {d.ingested_at
                ? t("v2.governance.policy.dec.ingestedLag", { lag: lagLabel(d.ingest_lag_ms) })
                : t("v2.governance.policy.dec.ingestedUnknown")}
            </span>
          )}
        </>
      ),
    },
    {
      key: "decision",
      title: t("v2.governance.policy.dec.colDecision"),
      className: "v2-decision-outcome",
      render: (d) => (
        <>
          <LinkButton onClick={() => setDetail(d)} testId={`v2-decision-open-${d.id}`} title={t("v2.governance.policy.dec.openDetail")}>
            <DecisionTag decision={d.decision} />
          </LinkButton>
          {d.log_only_would && (
            // N17-obs508: a LOG_ONLY policy would have denied this allowed call
            <Tag tone="orange" title={t("v2.governance.policy.dec.logOnlyWouldHint")} testId={`v2-decision-log-only-would-${d.id}`}>
              {t("v2.governance.policy.dec.logOnlyWould")}
            </Tag>
          )}
          {d.reason && <span className="sub clip">{d.reason}</span>}
        </>
      ),
    },
    {
      key: "action",
      title: t("v2.governance.policy.dec.colAction"),
      className: "v2-decision-resource",
      render: (d) => (
        <>
          <span className="mono">{d.action}</span>
          <span className="sub mono v2-governance-break">
            {d.resource_kind}: {d.resource_id ?? "—"}
          </span>
        </>
      ),
    },
    {
      key: "principal",
      title: t("v2.governance.policy.dec.colPrincipal"),
      className: "v2-decision-principal",
      render: (d) => (
        <>
          <span className="mono v2-governance-break">{d.principal_label ?? d.principal_id ?? "—"}</span>
          <span className="sub">{d.principal_kind}</span>
        </>
      ),
    },
    {
      key: "agent",
      title: t("v2.governance.policy.dec.colAgent"),
      className: "v2-decision-identity",
      render: (d) =>
        d.agent_id ? (
          <Link className="v2-governance-link" to={`/v2/agents?view=detail&agent=${encodeURIComponent(d.agent_id)}`}>
            {agentName(d.agent_id)}
          </Link>
        ) : (
          <span className="v2-muted">—</span>
        ),
    },
    {
      key: "policies",
      title: t("v2.governance.policy.dec.colPolicies"),
      render: (d) => (
        <div className="v2-tags v2-decision-policies">
          {d.policy_ids.length === 0 && <span className="v2-muted">—</span>}
          {d.policy_ids.map((id) => (
            <Tag key={id} tone="outline" title={names[id] ? `${id} — ${names[id]}` : id}>
              <span className="mono">{id}</span>
            </Tag>
          ))}
        </div>
      ),
    },
    {
      key: "mode",
      title: t("v2.governance.policy.dec.colMode"),
      className: "nowrap",
      render: (d) => (
        <>
          <PolicyModeTag mode={d.mode} />
          {d.latency_ms != null && <span className="sub mono">{d.latency_ms} ms</span>}
        </>
      ),
    },
    {
      key: "session",
      title: t("v2.governance.policy.dec.colSession"),
      className: "right",
      render: (d) =>
        d.session_id ? (
          <Link className="v2-governance-link" to={classicObservabilityToV2(`?session=${encodeURIComponent(d.session_id)}`)}>
            {d.session_id.slice(0, 8)}
          </Link>
        ) : (
          <span className="v2-muted">—</span>
        ),
    },
    ...(gateway ? gatewayColumns : []),
  ];

  return (
    <>
      <div className="v2-toolbar">
        <Button onClick={() => setNonce((n) => n + 1)} testId="v2-policy-dec-refresh">
          {t("v2.common.refresh")}
        </Button>
        <FilterSelect
          label={t("v2.common.timeRange")}
          value={range}
          onChange={(v) => setRange(v as V2Range)}
          options={RANGES.map((r) => ({ value: r, label: rangeLabel(t, r) }))}
          testId="v2-policy-dec-range"
        />
        <FilterSelect
          label={t("v2.policy.dec.colGateway")}
          value={gatewayId}
          allLabel={t("v2.common.all")}
          onChange={setGatewayId}
          options={gatewayFilterOptions}
          testId="v2-policy-dec-gateway"
        />
        <FilterSelect
          label={t("v2.governance.policy.dec.source")}
          value={source}
          allLabel={t("v2.common.all")}
          onChange={setSource}
          options={sources.map((s) => ({ value: s, label: t(`v2.governance.policy.source.${s}`) }))}
          testId="v2-policy-dec-source"
        />
        <FilterSelect
          label={t("v2.governance.policy.dec.colDecision")}
          value={decision}
          allLabel={t("v2.common.all")}
          onChange={setDecision}
          options={OUTCOMES.map((o) => ({ value: o, label: t(`v2.governance.policy.decision.${o}`) }))}
        />
        <FilterSelect
          label={t("v2.governance.policy.dec.colAgent")}
          value={agentId}
          allLabel={t("v2.common.all")}
          onChange={setAgentId}
          options={(entities?.agents ?? []).map((a) => ({ value: a.id, label: a.name }))}
        />
        <FilterSelect
          label={t("v2.governance.policy.dec.colPrincipal")}
          value={principal}
          allLabel={t("v2.common.all")}
          onChange={setPrincipal}
          options={[
            ...(entities?.users ?? []).map((u) => ({ value: u.username, label: u.username, group: t("v2.governance.policy.principal.user") })),
            ...(entities?.api_keys ?? []).map((k) => ({ value: k.id, label: k.name, group: t("v2.governance.policy.principal.api_key") })),
          ]}
        />
        <div className="end">
          <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
        </div>
      </div>
      {data && (
        <div className="v2-kpis">
          <Kpi label={t("v2.governance.policy.decision.allow")} value={totals.allow} tone="good" />
          <Kpi label={t("v2.governance.policy.decision.would_deny")} value={totals.wouldDeny} sub={t("v2.policy.dec.kpiLogOnlySub")} />
          <Kpi label={t("v2.governance.policy.decision.deny")} value={totals.deny} tone={totals.deny > 0 ? "bad" : undefined} />
          <Kpi label={t("v2.governance.policy.decision.error")} value={totals.error} tone={totals.error > 0 ? "bad" : undefined} />
        </div>
      )}
      <Card title={t("v2.governance.policy.dec.title")} sub={t("v2.policy.dec.sub", { name: engine.name })} testId="v2-policy-decisions">
        {/* N17-obs499: the CloudWatch mirror carries the (historic) RUNTIME-GUARD rows only — the
            banner is about that source, so it shows for "all sources" and "runtime", never while
            the operator looks at Gateway interceptor or historic hub rows */}
        {data?.runtime_channel && data.runtime_channel.status !== "ready" && (source === "" || source === "runtime") && (
          <Alert tone="warn" testId="v2-policy-dec-runtime-channel">
            <b>{t("v2.governance.policy.dec.runtimeChannel")}</b>{" "}
            {t(`v2.governance.policy.dec.${gateway ? "runtimeChannelStatusWithGateway" : "runtimeChannelStatus"}.${data.runtime_channel.status}`, {
              defaultValue: data.runtime_channel.status,
            })}
            {data.runtime_channel.reason ? ` — ${data.runtime_channel.reason}` : ""}
            {data.runtime_channel.log_group && (
              <>
                {" · "}
                {t("v2.governance.policy.dec.runtimeChannelGroup")} <span className="mono">{data.runtime_channel.log_group}</span>
              </>
            )}
          </Alert>
        )}
        {error ? (
          <Alert tone="error" action={<LinkButton onClick={reload}>{t("v2.common.retry")}</LinkButton>}>
            {error}
          </Alert>
        ) : (
          <>
            <div className="v2-decision-table" role="region" aria-label={t("v2.governance.policy.dec.title")} tabIndex={0}>
              <Table
                columns={columns}
                rows={paged.slice}
                rowKey={(d) => d.id}
                loading={loading}
                empty={t("v2.governance.policy.dec.empty", { range: rangeLabel(t, range) })}
              />
            </div>
            {rows.length > 0 && <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />}
            {rows.length >= LIMIT && <div className="v2-governance-foot">{t("v2.governance.policy.dec.truncated", { count: LIMIT })}</div>}
          </>
        )}
      </Card>
      <Drawer open={detail !== null} onClose={() => setDetail(null)} title={t("v2.governance.policy.dec.detailTitle")} testId="v2-decision-detail">
        {detail && <DecisionDetail row={detail} names={names} agentName={agentName} />}
      </Drawer>
    </>
  );
}

/**
 * One decision in full (A6): every column of the row, and — for a `deny` / `would_deny` —
 * the same card Chat shows, naming the denying policy and the engine stage with the raw
 * English engine text collapsed under its toggle.
 */
/** N17-obs482: the ingest lag of a gateway row as `+1.2 s` / `+640 ms`; `—` when unknown. */
function lagLabel(ms: number | null | undefined): string {
  if (ms == null) return "—";
  return Math.abs(ms) >= 1000 ? `+${(ms / 1000).toFixed(1)} s` : `+${Math.round(ms)} ms`;
}

function DecisionDetail({ row, names, agentName }: { row: PolicyDecision; names: Record<string, string>; agentName: (id: string | null) => string }) {
  const { t } = useTranslation();
  const dec = (key: string) => t(`v2.governance.policy.dec.${key}`);
  const gateway = row.source === "gateway";
  const items: { label: string; value: React.ReactNode }[] = [
    { label: dec("colTime"), value: <span className="mono">{fmtTime(row.created_at)}</span> },
    ...(gateway
      ? [{
          label: dec("colIngested"),
          value: row.ingested_at ? (
            <span className="mono" data-testid="v2-decision-detail-ingested">
              {fmtTime(row.ingested_at)} <span className="sub">{t("v2.governance.policy.dec.ingestedLag", { lag: lagLabel(row.ingest_lag_ms) })}</span>
            </span>
          ) : (
            <span className="v2-muted">{dec("ingestedUnknown")}</span>
          ),
        }]
      : []),
    {
      label: dec("source"),
      value: (
        <span className={isHistoric(row.source) ? "v2-muted" : undefined}>
          {t(`v2.governance.policy.source.${row.source}`, { defaultValue: row.source })} · {row.channel}
          {isHistoric(row.source) && <Tag tone="gray" testId="v2-decision-detail-historic">{dec("historic")}</Tag>}
        </span>
      ),
    },
    { label: dec("colDecision"), value: <DecisionTag decision={row.decision} /> },
    {
      label: dec("colStage"),
      value: row.stage || gateway ? (
        <span data-testid="v2-decision-detail-stage">
          {t(`v2.governance.policy.dec.stage.${row.stage ?? "final"}`, { defaultValue: row.stage ?? "—" })}
          {row.event_seq != null && <span className="sub mono"> #{row.event_seq}</span>}
        </span>
      ) : (
        <span className="v2-muted">—</span>
      ),
    },
    { label: dec("colAction"), value: <span className="mono v2-governance-break">{row.action} · {row.resource_kind}: {row.resource_id ?? "—"}</span> },
    { label: dec("colPrincipal"), value: <span className="mono v2-governance-break">{row.principal_label ?? row.principal_id ?? "—"} <span className="sub">{row.principal_kind}</span></span> },
    { label: dec("colAgent"), value: row.agent_id ? <span>{agentName(row.agent_id)}</span> : <span className="v2-muted">—</span> },
    {
      label: dec("colPolicies"),
      value: row.policy_ids.length ? (
        <div className="v2-tags" data-testid="v2-decision-detail-policies">
          {row.policy_ids.map((id) => (
            <Tag key={id} tone="outline">
              {names[id] ? <span>{names[id]} </span> : null}
              <span className="mono">{id}{row.policy_versions[id] != null ? `:v${row.policy_versions[id]}` : ""}</span>
            </Tag>
          ))}
        </div>
      ) : (
        <span className="v2-muted">—</span>
      ),
    },
    { label: dec("colMode"), value: <span><PolicyModeTag mode={row.mode} />{row.latency_ms != null && <span className="sub mono"> {row.latency_ms} ms</span>}</span> },
    { label: dec("colSession"), value: row.session_id ? <span className="mono">{row.session_id}</span> : <span className="v2-muted">—</span> },
  ];
  if (gateway) {
    items.push(
      { label: t("v2.policy.dec.colGateway"), value: row.gateway_id ? <span className="mono" data-testid="v2-decision-detail-gateway">{row.gateway_name ?? row.gateway_id}</span> : <span className="v2-muted">—</span> },
    );
    items.push(
      { label: dec("colPsid"), value: row.session_psid ? <span className="mono">{row.session_psid}</span> : <span className="v2-muted">—</span> },
      { label: dec("colInputKeys"), value: (row.input_keys ?? []).length ? <span className="mono">{(row.input_keys ?? []).join(", ")}</span> : <span className="v2-muted">—</span> },
    );
  }
  items.push({ label: dec("decisionId"), value: <span className="mono">{row.id}</span> });
  const denied = row.decision === "deny" || row.decision === "would_deny" || row.log_only_would === true;
  return (
    <div className="v2-stack">
      {denied && <PolicyDenyCard deny={decisionPolicyDeny(row, names)} />}
      <Descriptions one items={items} />
      {!denied && row.reason && (
        <div className="v2-muted" style={{ fontSize: 12.5 }}>
          {dec("reason")}: <code>{row.reason}</code>
        </div>
      )}
    </div>
  );
}

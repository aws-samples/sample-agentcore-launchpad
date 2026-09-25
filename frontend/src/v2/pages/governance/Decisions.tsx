import { ExternalLink } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  api,
  type GovernanceDecisionResponse,
  type GovernanceEvidenceRange,
  type GovernanceGatewayDetail,
  type GovernancePolicyDecision,
  type GovernancePolicyListResponse,
} from "../../../lib/api";
import { governanceError } from "../../../lib/governance";
import { fmtTime, RANGES, rangeLabel } from "../../format";
import { useLoad, usePaged } from "../../hooks";
import { Alert, Button, Card, type Column, FilterSelect, Kpi, LinkButton, Pager, Table, Tag } from "../../ui";
import { classicObservabilityToV2 } from "../observability/classicUrl";
import { StatusTag } from "./widgets";

/** CloudWatch-metric aggregates. Metrics carry no principal, reason or trace id — a count surface. */
function EvidenceAggregates({ data }: { data: GovernanceDecisionResponse }) {
  const { t } = useTranslation();
  const groups = [
    {
      label: t("v2.governance.dec.byOperation"),
      rows: data.by_operation.map((row) => ({
        key: row.operation,
        note: t(`governance.decisions.basis.${row.basis}`),
        allow: row.allow,
        deny: row.deny,
      })),
    },
    { label: t("v2.governance.dec.byMode"), rows: data.by_mode.map((row) => ({ key: row.mode, note: "", allow: row.allow, deny: row.deny })) },
    { label: t("v2.governance.dec.byPolicy"), rows: data.by_policy.map((row) => ({ key: row.policy_id, note: "", allow: row.allow, deny: row.deny })) },
    { label: t("v2.governance.dec.byTool"), rows: data.by_tool.map((row) => ({ key: row.tool, note: "", allow: row.allow, deny: row.deny })) },
  ].filter((group) => group.rows.length > 0);

  return (
    <Card title={t("v2.governance.dec.aggregates")} sub={t("governance.decisions.sourceMetrics")} testId="v2-governance-aggregates">
      {data.truncated && <Alert tone="warn">{t("governance.decisions.truncated")}</Alert>}
      {data.policy_filter_partial && <Alert tone="warn">{t("governance.decisions.policyFilterPartial")}</Alert>}
      <div className="v2-governance-agg">
        {groups.map((group) => (
          <div key={group.label} className="v2-governance-agg-group">
            <div className="v2-sub-title" style={{ marginTop: 0 }}>
              {group.label}
            </div>
            {group.rows.map((row) => (
              <div key={row.key} className="v2-governance-agg-row">
                <span className="mono k" title={row.key}>
                  {row.key}
                  {row.note && <span className="v2-muted"> · {row.note}</span>}
                </span>
                <span className="v2-row" style={{ gap: 6, flexWrap: "nowrap" }}>
                  <Tag tone="green">{row.allow}</Tag>
                  <Tag tone="red">{row.deny}</Tag>
                </span>
              </div>
            ))}
          </div>
        ))}
      </div>
      <div className="v2-muted" style={{ fontSize: 12.5, marginTop: 12 }}>
        {t("governance.decisions.aggregateNote")}
      </div>
    </Card>
  );
}

type Row = GovernancePolicyDecision & { _i: number };

/** Policy decisions of one Gateway over a window (aggregates + per-decision span rows). */
export function DecisionsSection({
  gateway,
  policies,
  refreshTick,
}: {
  gateway: GovernanceGatewayDetail;
  policies: GovernancePolicyListResponse | null;
  refreshTick: number;
}) {
  const { t } = useTranslation();
  const [range, setRange] = useState<GovernanceEvidenceRange>("24h");
  const [policyId, setPolicyId] = useState("");
  const [force, setForce] = useState(0);
  const { data, loading, error, reload } = useLoad(
    () =>
      api
        .governanceDecisions(gateway.id, range, policyId || undefined, force > 0)
        .catch((err: unknown) => Promise.reject(new Error(governanceError(err)))),
    `gov-decisions:${gateway.id}:${range}:${policyId}:${force}:${refreshTick}`,
  );
  // decision rows carry no id of their own; the list position keys them
  const decisions = useMemo<Row[]>(() => (data?.decisions ?? []).map((d, i) => ({ ...d, _i: i })), [data]);
  const paged = usePaged(decisions, 15);

  const columns: Column<Row>[] = [
    {
      key: "time",
      title: t("v2.governance.dec.colTime"),
      className: "nowrap",
      render: (d) => (
        <>
          {fmtTime(d.at)}
          <span className="sub">{t(`governance.decisions.evaluation.${d.evaluation}`)}</span>
        </>
      ),
    },
    {
      key: "outcome",
      title: t("v2.governance.dec.colOutcome"),
      render: (d) => (
        <>
          <StatusTag status={d.outcome} />
          {d.reason && <span className="sub clip">{d.reason}</span>}
        </>
      ),
    },
    { key: "action", title: t("v2.governance.exactAction"), render: (d) => <span className="mono v2-governance-break">{d.action ?? "—"}</span> },
    {
      key: "principal",
      title: t("v2.governance.dec.colPrincipal"),
      render: (d) =>
        d.principal ? (
          <span className="mono v2-governance-break">{d.principal}</span>
        ) : (
          <span className="v2-muted v2-governance-dotted" title={t("governance.decisions.principalAbsentWhy")}>
            {t("governance.decisions.principalAbsent")}
          </span>
        ),
    },
    {
      key: "policy",
      title: t("v2.governance.colPolicy"),
      render: (d) => (
        <>
          <span className="mono v2-governance-break">{d.policy_id ?? "—"}</span>
          {d.log_only_matched_policies.length > 0 && (
            <span className="sub">{t("governance.decisions.logOnlyMatched", { policies: d.log_only_matched_policies.join(", ") })}</span>
          )}
        </>
      ),
    },
    {
      key: "modes",
      title: t("v2.governance.dec.colModes"),
      render: (d) => (
        <div className="v2-tags">
          <StatusTag status={d.engine_mode} />
          <StatusTag status={d.policy_mode} />
        </div>
      ),
    },
    {
      key: "trace",
      title: t("v2.governance.dec.colTrace"),
      className: "right",
      render: (d) =>
        d.trace_id ? (
          <Link className="v2-governance-link" to={classicObservabilityToV2(`?trace=${encodeURIComponent(d.trace_id)}`)}>
            {d.trace_id.slice(0, 8)}
            <ExternalLink size={12} aria-hidden="true" />
          </Link>
        ) : d.session_id ? (
          <Link className="v2-governance-link" to={classicObservabilityToV2(`?session=${encodeURIComponent(d.session_id)}`)}>
            {d.session_id.slice(0, 8)}
            <ExternalLink size={12} aria-hidden="true" />
          </Link>
        ) : (
          <span className="v2-muted">—</span>
        ),
    },
  ];

  const notes: string[] = [];
  if (data?.spans_unavailable_reason) notes.push(t("governance.decisions.spansUnavailable", { reason: data.spans_unavailable_reason }));
  if (data?.span_channel_status === "missing") notes.push(t("governance.decisions.spanChannelMissing", { reason: data.span_channel_reason }));
  if (data?.span_channel_status === "unknown") notes.push(t("governance.decisions.spanChannelUnknown", { reason: data.span_channel_reason }));
  if (decisions.some((d) => d.evaluation === "tool_listing")) notes.push(t("governance.decisions.listingRowsNote"));

  const toolbar = (
    <div className="v2-toolbar">
      <Button onClick={() => (force ? reload() : setForce(1))} testId="v2-governance-dec-refresh">
        {t("v2.common.refresh")}
      </Button>
      <FilterSelect
        label={t("v2.common.timeRange")}
        value={range}
        onChange={(v) => setRange(v as GovernanceEvidenceRange)}
        options={RANGES.map((r) => ({ value: r, label: rangeLabel(t, r) }))}
        testId="v2-governance-dec-range"
      />
      <FilterSelect
        label={t("v2.governance.colPolicy")}
        value={policyId}
        allLabel={t("v2.common.all")}
        onChange={setPolicyId}
        options={(policies?.policies ?? []).map((p) => ({ value: p.id, label: p.name }))}
      />
      <div className="end">
        {data?.cache && <span className="v2-count">{t("governance.decisions.cache", { age: Math.round(data.cache.age_seconds) })}</span>}
      </div>
    </div>
  );

  return (
    <>
      {toolbar}
      {data && data.available && (
        <div className="v2-kpis">
          <Kpi label={t("v2.governance.dec.kpiEvidence")} value={data.evidence_count} sub={rangeLabel(t, data.range)} />
          <Kpi
            label={t("v2.governance.dec.kpiLogOnly")}
            value={data.log_only_count}
            tone={data.log_only_count > 0 ? "good" : undefined}
            sub={t("v2.governance.dec.kpiLogOnlySub")}
          />
          <Kpi label={t("v2.governance.dec.kpiAllow")} value={data.totals.allow} tone="good" />
          <Kpi label={t("v2.governance.dec.kpiDeny")} value={data.totals.deny} tone={data.totals.deny > 0 ? "bad" : undefined} />
        </div>
      )}
      {data && data.available && data.evidence_count > 0 && <EvidenceAggregates data={data} />}
      <Card title={t("v2.governance.dec.title")} sub={t("governance.decisions.awsSource")} testId="v2-governance-decisions">
        {error ? (
          <Alert tone="error" action={<LinkButton onClick={reload}>{t("v2.common.retry")}</LinkButton>}>
            {error}
          </Alert>
        ) : data && !data.available ? (
          <Alert tone="warn" action={<LinkButton onClick={reload}>{t("v2.common.retry")}</LinkButton>}>
            <b>{t("v2.governance.dec.unavailable")}</b> {data.unavailable_reason}
          </Alert>
        ) : data && data.evidence_count === 0 ? (
          <Alert>
            <b>{t("v2.governance.dec.noEvidence")}</b> {t("governance.decisions.noEvidenceHint", { range: data.range })}
          </Alert>
        ) : (
          <>
            <Table
              columns={columns}
              rows={paged.slice}
              rowKey={(d) => String(d._i)}
              loading={loading}
              empty={t("v2.governance.dec.noRows")}
            />
            {decisions.length > 0 && <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />}
          </>
        )}
        {notes.map((note) => (
          <div key={note} className="v2-governance-foot">
            {note}
          </div>
        ))}
      </Card>
    </>
  );
}

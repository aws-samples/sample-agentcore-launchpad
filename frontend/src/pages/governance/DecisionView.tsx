import { ArrowLeft, ExternalLink, RefreshCw, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { Btn, Chip, DataTable, Panel } from "../../components";
import {
  api,
  type GovernanceDecisionResponse,
  type GovernanceEvidenceRange,
  type GovernanceGatewayDetail,
  type GovernancePolicyListResponse,
} from "../../lib/api";

import {
  formatTimestamp,
  governanceError,
  statusTone,
  type GovernanceView,
} from "./types";

interface Props {
  gatewayId: string;
  onNavigate: (view: GovernanceView, gatewayId?: string, policyId?: string) => void;
}

const RANGES: GovernanceEvidenceRange[] = ["1h", "6h", "24h", "7d"];

/** Aggregate evidence from CloudWatch policy metrics. Metrics cannot carry a
 *  principal, reason, or trace id, so this is a count surface — per-decision rows
 *  come from Policy spans and stay empty until those are enabled. */
function EvidenceAggregates({ data }: { data: GovernanceDecisionResponse | null }) {
  const { t } = useTranslation();
  if (!data || data.evidence_count === 0) return null;

  const groups: { label: string; rows: { key: string; note?: string; allow: number; deny: number }[] }[] = [
    {
      label: t("governance.decisions.byOperation"),
      rows: data.by_operation.map((row) => ({
        key: row.operation,
        note: t(`governance.decisions.basis.${row.basis}`),
        allow: row.allow,
        deny: row.deny,
      })),
    },
    {
      label: t("governance.decisions.byMode"),
      rows: data.by_mode.map((row) => ({ key: row.mode, allow: row.allow, deny: row.deny })),
    },
    {
      label: t("governance.decisions.byPolicy"),
      rows: data.by_policy.map((row) => ({
        key: row.policy_id,
        allow: row.allow,
        deny: row.deny,
      })),
    },
    {
      label: t("governance.decisions.byTool"),
      rows: data.by_tool.map((row) => ({ key: row.tool, allow: row.allow, deny: row.deny })),
    },
  ];

  return (
    <div className="gov-evidence">
      <div className="gov-evidence-head">
        <Chip tone="good">
          {t("governance.decisions.evidenceCount", { count: data.evidence_count })}
        </Chip>
        <Chip tone={data.log_only_count > 0 ? "good" : "warn"}>
          {t("governance.decisions.logOnlyCount", { count: data.log_only_count })}
        </Chip>
        <span className="gov-cell-note">
          {t("governance.decisions.totals", {
            allow: data.totals.allow,
            deny: data.totals.deny,
          })}
        </span>
        <span className="gov-cell-note">{t("governance.decisions.sourceMetrics")}</span>
      </div>
      {data.truncated ? (
        <div className="gov-cell-note">{t("governance.decisions.truncated")}</div>
      ) : null}
      {data.policy_filter_partial ? (
        <div className="gov-cell-note">{t("governance.decisions.policyFilterPartial")}</div>
      ) : null}
      <div className="gov-evidence-groups">
        {groups
          .filter((group) => group.rows.length > 0)
          .map((group) => (
            <div className="gov-evidence-group" key={group.label}>
              <strong>{group.label}</strong>
              {group.rows.map((row) => (
                <div className="gov-evidence-row" key={`${group.label}-${row.key}`}>
                  <span className="mono gov-break">{row.key}</span>
                  {row.note ? <span className="gov-cell-note">{row.note}</span> : null}
                  <span className="mono">
                    {t("governance.decisions.allowDeny", { allow: row.allow, deny: row.deny })}
                  </span>
                </div>
              ))}
            </div>
          ))}
      </div>
      <div className="gov-cell-note">{t("governance.decisions.aggregateNote")}</div>
    </div>
  );
}

export function DecisionView({ gatewayId, onNavigate }: Props) {
  const { t, i18n } = useTranslation();
  const [gateway, setGateway] = useState<GovernanceGatewayDetail | null>(null);
  const [policies, setPolicies] = useState<GovernancePolicyListResponse | null>(null);
  const [data, setData] = useState<GovernanceDecisionResponse | null>(null);
  const [range, setRange] = useState<GovernanceEvidenceRange>("24h");
  const [policyId, setPolicyId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(
    async (force = false) => {
      setRefreshing(true);
      setError(null);
      const [gatewayResult, policiesResult, decisionsResult] = await Promise.allSettled([
        api.getGovernanceGateway(gatewayId),
        api.listGovernancePolicies(gatewayId),
        api.governanceDecisions(gatewayId, range, policyId || undefined, force),
      ]);
      if (gatewayResult.status === "fulfilled") setGateway(gatewayResult.value);
      if (policiesResult.status === "fulfilled") setPolicies(policiesResult.value);
      if (decisionsResult.status === "fulfilled") {
        setData(decisionsResult.value);
      } else {
        setError(governanceError(decisionsResult.reason));
      }
      if (gatewayResult.status === "rejected") {
        setError(governanceError(gatewayResult.reason));
      }
      setRefreshing(false);
    },
    [gatewayId, policyId, range],
  );

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <>
      <div className="gov-toolbar">
        <Btn onClick={() => onNavigate("gateway", gatewayId)}>
          <ArrowLeft size={14} aria-hidden="true" />
          {t("governance.actions.back")}
        </Btn>
        <div className="gov-toolbar-title">
          <strong>{t("governance.decisions.title")}</strong>
          <span>{gateway?.name ?? gatewayId}</span>
        </div>
        <div className="range" aria-label={t("governance.decisions.range")}>
          {RANGES.map((value) => (
            <button
              type="button"
              key={value}
              className={range === value ? "on" : ""}
              onClick={() => setRange(value)}
            >
              {value}
            </button>
          ))}
        </div>
        <Btn disabled={refreshing} onClick={() => void load(true)}>
          <RefreshCw size={14} aria-hidden="true" />
          {t("governance.actions.refresh")}
        </Btn>
      </div>

      <Panel
        brk
        title={t("governance.decisions.awsTitle")}
        sub={t("governance.decisions.awsSource")}
        pad={false}
        end={
          <div className="gov-filter">
            <label htmlFor="governance-policy-filter">
              {t("governance.decisions.policy")}
            </label>
            <select
              id="governance-policy-filter"
              className="input"
              value={policyId}
              onChange={(event) => setPolicyId(event.target.value)}
            >
              <option value="">{t("governance.decisions.allPolicies")}</option>
              {policies?.policies.map((policy) => (
                <option key={policy.id} value={policy.id}>
                  {policy.name}
                </option>
              ))}
            </select>
          </div>
        }
      >
        {error ? (
          <div className="gov-state gov-state-error">
            <TriangleAlert size={20} aria-hidden="true" />
            <strong>{t("governance.states.unavailable")}</strong>
            <span>{error}</span>
            <Btn onClick={() => void load(true)}>{t("governance.actions.retry")}</Btn>
          </div>
        ) : data && !data.available ? (
          <div className="gov-state gov-state-warn">
            <TriangleAlert size={20} aria-hidden="true" />
            <strong>{t("governance.decisions.telemetryUnavailable")}</strong>
            <span>{data.unavailable_reason}</span>
            <Btn onClick={() => void load(true)}>{t("governance.actions.retry")}</Btn>
          </div>
        ) : data && data.evidence_count === 0 ? (
          <div className="gov-state">
            <strong>{t("governance.decisions.noEvidenceInWindow")}</strong>
            <span>{t("governance.decisions.noEvidenceHint", { range: data.range })}</span>
          </div>
        ) : (
          <>
          <EvidenceAggregates data={data} />
          <DataTable
            columns={[
              { key: "time", label: t("governance.decisions.time") },
              { key: "outcome", label: t("governance.decisions.outcome") },
              { key: "action", label: t("governance.decisions.action") },
              { key: "principal", label: t("governance.decisions.principal") },
              { key: "policy", label: t("governance.decisions.policy") },
              { key: "modes", label: t("governance.decisions.modes") },
              { key: "trace", label: t("governance.decisions.trace") },
            ]}
            isEmpty={!refreshing && (data?.decisions.length ?? 0) === 0}
            empty={t("governance.decisions.noAwsEvidence")}
          >
            {refreshing && !data ? (
              <tr>
                <td colSpan={7} className="loading-line">
                  {t("common.loading")}
                </td>
              </tr>
            ) : null}
            {data?.decisions.map((decision, index) => (
              <tr key={`${decision.at}-${decision.policy_id ?? "none"}-${index}`}>
                <td className="mono">
                  {formatTimestamp(decision.at, i18n.language)}
                  <div className="gov-cell-note">
                    {t(`governance.decisions.evaluation.${decision.evaluation}`)}
                  </div>
                </td>
                <td>
                  <Chip tone={statusTone(decision.outcome)}>{decision.outcome}</Chip>
                </td>
                <td className="pri mono gov-break">{decision.action}</td>
                <td className="mono gov-break">
                  {decision.principal ?? (
                    <span className="gov-absent" title={t("governance.decisions.principalAbsentWhy")}>
                      {t("governance.decisions.principalAbsent")}
                    </span>
                  )}
                </td>
                <td className="mono">
                  {decision.policy_id ?? "-"}
                  {decision.log_only_matched_policies.length > 0 ? (
                    <div className="gov-cell-note">
                      {t("governance.decisions.logOnlyMatched", {
                        policies: decision.log_only_matched_policies.join(", "),
                      })}
                    </div>
                  ) : null}
                </td>
                <td>
                  <div className="gov-action-list">
                    <Chip tone={statusTone(decision.engine_mode)}>
                      {decision.engine_mode ?? "-"}
                    </Chip>
                    <Chip tone={statusTone(decision.policy_mode)}>
                      {decision.policy_mode ?? "-"}
                    </Chip>
                  </div>
                </td>
                <td>
                  {decision.trace_id ? (
                    <Link
                      className="gov-text-link"
                      to={`/observability?trace=${encodeURIComponent(decision.trace_id)}`}
                    >
                      {decision.trace_id.slice(0, 8)}
                      <ExternalLink size={12} aria-hidden="true" />
                    </Link>
                  ) : decision.session_id ? (
                    <Link
                      className="gov-text-link"
                      to={`/observability?session=${encodeURIComponent(decision.session_id)}`}
                    >
                      {decision.session_id.slice(0, 8)}
                      <ExternalLink size={12} aria-hidden="true" />
                    </Link>
                  ) : (
                    "-"
                  )}
                </td>
              </tr>
            ))}
          </DataTable>
          </>
        )}
        {data?.spans_unavailable_reason ? (
          <div className="gov-data-foot">
            <span>
              {t("governance.decisions.spansUnavailable", {
                reason: data.spans_unavailable_reason,
              })}
            </span>
          </div>
        ) : null}
        {data?.decisions.some((d) => d.evaluation === "tool_listing") ? (
          <div className="gov-data-foot">
            <span>{t("governance.decisions.listingRowsNote")}</span>
          </div>
        ) : null}
        {data?.cache ? (
          <div className="gov-data-foot">
            <span>
              {t("governance.decisions.cache", {
                age: Math.round(data.cache.age_seconds),
              })}
            </span>
            <span>{t("governance.decisions.count", { count: data.count })}</span>
          </div>
        ) : null}
      </Panel>
    </>
  );
}

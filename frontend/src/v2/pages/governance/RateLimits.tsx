import { ChevronDown, ChevronRight, Plus } from "lucide-react";
import { Fragment, useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, type GovernanceGatewayDetail, type GovernanceRateLimit } from "../../../lib/api";
import { governanceError, isGatewayReady } from "../../../lib/governance";
import { entryMetricSummary } from "../../../lib/governanceRateLimits";
import { fmtTime } from "../../format";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, LinkButton, Spin, Tag } from "../../ui";
import { StatusTag } from "./widgets";

/** Gateway rate limits: list with expandable entries; create / edit open the editor sub-page. */
export function RateLimitsSection({
  gateway,
  operationBusy,
  refreshTick,
}: {
  gateway: GovernanceGatewayDetail;
  operationBusy: boolean;
  refreshTick: number;
}) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const [limits, setLimits] = useState<GovernanceRateLimit[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [toDelete, setToDelete] = useState<GovernanceRateLimit | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await api.listGovernanceRateLimits(gateway.id);
      setLimits(result.rate_limits);
      setLoadError(null);
    } catch (error) {
      setLimits((current) => current ?? []);
      setLoadError(governanceError(error));
    }
  }, [gateway.id]);

  useEffect(() => {
    void load();
  }, [load, refreshTick]);

  // mutation gates, in the order the operator can fix them
  const gatewayBlockers: string[] = [];
  if (!gateway.managed) gatewayBlockers.push(t("governance.blockers.notManaged"));
  if (!isGatewayReady(gateway)) gatewayBlockers.push(t("governance.blockers.gatewayNotReady"));
  if (operationBusy || busy) gatewayBlockers.push(t("governance.blockers.busy"));
  const rowBlockers = (limit: GovernanceRateLimit): string[] =>
    limit.status.toUpperCase() !== "ACTIVE"
      ? [...gatewayBlockers, t("governance.rateLimits.blockers.statusNotActive", { status: limit.status })]
      : gatewayBlockers;

  const confirmDelete = async () => {
    if (!toDelete) return;
    const limit = toDelete;
    setBusy(true);
    try {
      await api.deleteGovernanceRateLimit(gateway.id, limit.id);
      toast("success", t("governance.rateLimits.deleted"));
      setToDelete(null);
      await load();
    } catch (error) {
      toast("error", governanceError(error));
    } finally {
      setBusy(false);
    }
  };

  const toggle = (id: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <Card
      title={t("v2.governance.section.rateLimits")}
      sub={t("governance.rateLimits.sub")}
      end={
        <Button
          kind="primary"
          size="sm"
          disabled={gatewayBlockers.length > 0}
          title={gatewayBlockers.join("; ") || undefined}
          onClick={() => setParams({ view: "rate-limit", gateway: gateway.id })}
          testId="v2-governance-rl-new"
        >
          <Plus size={14} aria-hidden="true" />
          {t("v2.governance.rl.add")}
        </Button>
      }
      testId="v2-governance-rate-limits"
    >
      <Alert>{t("governance.rateLimits.semantics")}</Alert>
      {loadError && (
        <Alert tone="error" action={<LinkButton onClick={() => void load()}>{t("v2.common.retry")}</LinkButton>}>
          {loadError}
        </Alert>
      )}
      <div className="v2-table-wrap">
        <table className="v2-table">
          <thead>
            <tr>
              <th>{t("v2.governance.rl.colId")}</th>
              <th>{t("v2.governance.rl.colKeys")}</th>
              <th>{t("v2.governance.rl.colEntries")}</th>
              <th>{t("v2.governance.colStatus")}</th>
              <th>{t("v2.governance.colUpdated")}</th>
              <th className="right">{t("v2.common.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {limits === null ? (
              <tr>
                <td colSpan={6}>
                  <Spin />
                </td>
              </tr>
            ) : limits.length === 0 ? (
              <tr>
                <td colSpan={6}>
                  <div className="v2-table-empty">{t("v2.governance.rl.empty")}</div>
                </td>
              </tr>
            ) : (
              limits.map((limit) => {
                const open = expanded.has(limit.id);
                const blockers = rowBlockers(limit);
                return (
                  <Fragment key={limit.id}>
                    <tr data-testid={`rate-limit-${limit.id}`}>
                      <td>
                        <button
                          type="button"
                          className="v2-governance-toggle mono"
                          aria-expanded={open}
                          aria-label={open ? t("v2.common.collapse") : t("v2.common.expand")}
                          onClick={() => toggle(limit.id)}
                        >
                          {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
                          {limit.id}
                        </button>
                        {limit.description && <span className="sub">{limit.description}</span>}
                      </td>
                      <td>
                        <div className="v2-tags">
                          {limit.dimension_keys.map((key) => (
                            <Tag key={key} tone="outline">
                              <span className="mono">{key}</span>
                            </Tag>
                          ))}
                        </div>
                      </td>
                      <td className="num">{limit.entries.length}</td>
                      <td>
                        <StatusTag status={limit.status} />
                      </td>
                      <td className="nowrap">{fmtTime(limit.updated_at)}</td>
                      <td className="right">
                        <div className="v2-actions">
                          <LinkButton
                            disabled={blockers.length > 0}
                            title={blockers.join("; ") || undefined}
                            onClick={() => setParams({ view: "rate-limit", gateway: gateway.id, limit: limit.id })}
                          >
                            {t("v2.common.edit")}
                          </LinkButton>
                          <LinkButton
                            danger
                            disabled={blockers.length > 0}
                            title={blockers.join("; ") || undefined}
                            onClick={() => setToDelete(limit)}
                          >
                            {t("v2.common.delete")}
                          </LinkButton>
                        </div>
                      </td>
                    </tr>
                    {open && (
                      <tr className="v2-governance-subrow">
                        <td colSpan={6}>
                          <table className="v2-table dense">
                            <thead>
                              <tr>
                                {limit.dimension_keys.map((key) => (
                                  <th key={key} className="mono">
                                    {key}
                                  </th>
                                ))}
                                <th>{t("v2.governance.rl.rate")}</th>
                              </tr>
                            </thead>
                            <tbody>
                              {limit.entries.map((entry, index) => (
                                <tr key={index}>
                                  {limit.dimension_keys.map((key) => (
                                    <td key={key} className="mono">
                                      {entry.dimensions[key] ?? "—"}
                                    </td>
                                  ))}
                                  <td>
                                    <div className="v2-tags">
                                      {entryMetricSummary(entry).map((label) => (
                                        <Tag key={label} tone="blue">
                                          {label}
                                        </Tag>
                                      ))}
                                    </div>
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })
            )}
          </tbody>
        </table>
      </div>
      <Confirm
        open={toDelete !== null}
        title={t("v2.governance.rl.confirmDeleteTitle")}
        body={t("governance.rateLimits.confirmDelete", { id: toDelete?.id ?? "", name: gateway.name })}
        confirmLabel={t("v2.common.delete")}
        danger
        busy={busy}
        onConfirm={() => void confirmDelete()}
        onClose={() => setToDelete(null)}
      />
    </Card>
  );
}

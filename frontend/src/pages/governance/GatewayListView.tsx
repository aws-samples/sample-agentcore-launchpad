import { ArrowRight, RefreshCw, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip, DataTable, Panel } from "../../components";
import { api, type GovernanceGatewayListResponse } from "../../lib/api";
import { governanceError, statusTone } from "./types";

interface Props {
  onOpen: (gatewayId: string) => void;
}

export function GatewayListView({ onOpen }: Props) {
  const { t } = useTranslation();
  const [data, setData] = useState<GovernanceGatewayListResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (force = false) => {
    setRefreshing(true);
    setError(null);
    try {
      setData(await api.listGovernanceGateways(force));
    } catch (loadError) {
      setError(governanceError(loadError));
    } finally {
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const gateways = data?.gateways ?? [];
  const sourceMeta = [data?.account_id, data?.region].filter(Boolean).join(" / ");

  return (
    <Panel
      brk
      title={t("governance.inventory.title")}
      sub={sourceMeta || t("governance.inventory.liveSource")}
      pad={false}
      end={
        <Btn
          disabled={refreshing}
          title={t("governance.actions.refresh")}
          onClick={() => void load(true)}
        >
          <RefreshCw size={14} aria-hidden="true" />
          {t("governance.actions.refresh")}
        </Btn>
      }
    >
      {error ? (
        <div className="gov-state gov-state-error">
          <TriangleAlert size={20} aria-hidden="true" />
          <strong>{t("governance.states.unavailable")}</strong>
          <span>{error}</span>
          <Btn onClick={() => void load(true)}>{t("governance.actions.retry")}</Btn>
        </div>
      ) : (
        <DataTable
          columns={[
            { key: "gateway", label: t("governance.inventory.gateway") },
            { key: "status", label: t("governance.inventory.status") },
            { key: "authorizer", label: t("governance.inventory.authorizer") },
            { key: "targets", label: t("governance.inventory.targets") },
            { key: "registry", label: t("governance.inventory.registry") },
            { key: "engine", label: t("governance.inventory.engine") },
            { key: "managed", label: t("governance.inventory.management") },
          ]}
          isEmpty={!refreshing && gateways.length === 0}
          empty={t("governance.inventory.empty")}
        >
          {refreshing && !data ? (
            <tr>
              <td colSpan={7} className="loading-line">
                {t("common.loading")}
              </td>
            </tr>
          ) : null}
          {gateways.map((gateway) => (
            <tr
              key={gateway.id}
              className="rowlink"
              tabIndex={0}
              onClick={() => onOpen(gateway.id)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") onOpen(gateway.id);
              }}
            >
              <td className="pri">
                <div>{gateway.name}</div>
                <span className="arn" title={gateway.arn}>
                  {gateway.id}
                </span>
              </td>
              <td>
                <Chip tone={statusTone(gateway.status)}>{gateway.status}</Chip>
              </td>
              <td className="mono">{gateway.authorizer_type}</td>
              <td className="mono">{gateway.target_count}</td>
              <td>
                {gateway.registry_record ? (
                  <Chip tone={statusTone(gateway.registry_record.status)}>
                    {gateway.registry_record.status}
                  </Chip>
                ) : (
                  <Chip tone="muted">{t("governance.states.notCataloged")}</Chip>
                )}
                <div className="gov-cell-note">
                  {gateway.attachability.attachable
                    ? t("governance.states.attachable")
                    : t("governance.states.catalogOnly")}
                </div>
              </td>
              <td>
                {gateway.policy_engine?.missing ? (
                  <>
                    <div className="mono">{gateway.policy_engine.id}</div>
                    <Chip tone="crit">{t("governance.states.engineDeleted")}</Chip>
                  </>
                ) : gateway.policy_engine ? (
                  <>
                    <div className="mono">{gateway.policy_engine.name}</div>
                    <Chip tone={statusTone(gateway.policy_engine.mode)}>
                      {gateway.policy_engine.mode ?? "-"}
                    </Chip>
                  </>
                ) : (
                  <Chip tone="muted">{t("governance.states.notAttached")}</Chip>
                )}
              </td>
              <td>
                <div className="gov-row-action">
                  <Chip tone={gateway.managed ? "good" : "muted"}>
                    {gateway.managed
                      ? t("governance.states.managed")
                      : t("governance.states.unmanaged")}
                  </Chip>
                  <ArrowRight size={14} aria-hidden="true" />
                </div>
              </td>
            </tr>
          ))}
        </DataTable>
      )}
    </Panel>
  );
}

import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, type GovernanceGatewaySummary } from "../../../lib/api";
import { governanceError } from "../../../lib/governance";
import { useLoad, usePaged } from "../../hooks";
import {
  Button,
  Card,
  type Column,
  FilterSelect,
  Kpi,
  LinkButton,
  Pager,
  SearchInput,
  Table,
  Tag,
} from "../../ui";
import { StatusTag } from "./widgets";

/** MCP Gateway inventory of the live AWS account / region. */
export function GatewayList() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const [force, setForce] = useState(0);
  // the first load may be served from the backend cache; REFRESH bypasses it
  const { data, loading, error, reload } = useLoad(
    () => api.listGovernanceGateways(force > 0).catch((err: unknown) => Promise.reject(new Error(governanceError(err)))),
    `gov-gateways:${force}`,
  );
  const [managed, setManaged] = useState("");
  const [q, setQ] = useState("");

  const gateways = useMemo(() => data?.gateways ?? [], [data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return gateways.filter((gateway) => {
      if (managed && String(gateway.managed) !== managed) return false;
      return !needle || `${gateway.name} ${gateway.id} ${gateway.description}`.toLowerCase().includes(needle);
    });
  }, [gateways, managed, q]);
  const paged = usePaged(rows, 12);

  const open = (gateway: GovernanceGatewaySummary) => setParams({ view: "gateway", gateway: gateway.id });
  const count = (fn: (g: GovernanceGatewaySummary) => boolean) => gateways.filter(fn).length;

  const columns: Column<GovernanceGatewaySummary>[] = [
    {
      key: "name",
      title: t("v2.governance.colGateway"),
      render: (gateway) => (
        <>
          <LinkButton onClick={() => open(gateway)} testId={`v2-governance-gw-${gateway.id}`}>
            <span className="ellipsis" style={{ maxWidth: 260 }} title={gateway.name}>
              {gateway.name}
            </span>
          </LinkButton>
          <span className="sub mono" title={gateway.arn}>
            ID: {gateway.id}
          </span>
        </>
      ),
    },
    {
      key: "status",
      title: t("v2.governance.colStatus"),
      render: (gateway) => (
        <>
          <StatusTag status={gateway.status} title={gateway.status_reasons.join("; ") || undefined} />
          <span className="sub mono" title={t("v2.governance.colAuthorizer")}>
            {gateway.authorizer_type}
          </span>
        </>
      ),
    },
    { key: "targets", title: t("v2.governance.colTargets"), className: "num", render: (gateway) => gateway.target_count },
    {
      key: "managed",
      title: t("v2.governance.colManaged"),
      render: (gateway) => (
        <Tag tone={gateway.managed ? "green" : "gray"} dot>
          {gateway.managed ? t("v2.governance.managed") : t("v2.governance.unmanaged")}
        </Tag>
      ),
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (gateway) => (
        <div className="v2-actions">
          <LinkButton onClick={() => open(gateway)}>{t("v2.common.view")}</LinkButton>
        </div>
      ),
    },
  ];

  const source = [data?.account_id, data?.region].filter(Boolean).join(" / ");

  return (
    <>
      <div className="v2-kpis">
        <Kpi label={t("v2.governance.kpi.gateways")} value={data ? gateways.length : "—"} sub={source || undefined} />
        <Kpi label={t("v2.governance.kpi.managed")} value={data ? count((g) => g.managed) : "—"} />
      </div>
      <Card>
        <div className="v2-toolbar">
          <Button onClick={() => (force ? reload() : setForce(1))} testId="v2-governance-refresh">
            {t("v2.common.refresh")}
          </Button>
          <FilterSelect
            label={t("v2.governance.colManaged")}
            value={managed}
            allLabel={t("v2.common.all")}
            onChange={setManaged}
            options={[
              { value: "true", label: t("v2.governance.managed") },
              { value: "false", label: t("v2.governance.unmanaged") },
            ]}
          />
          <div className="end">
            <SearchInput value={q} onChange={setQ} placeholder={t("v2.governance.search")} />
            <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
          </div>
        </div>
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(gateway) => gateway.id}
          loading={loading}
          error={error}
          onRetry={reload}
          empty={gateways.length ? t("v2.governance.noMatch") : t("v2.governance.empty")}
          testId="v2-governance-gateways"
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
    </>
  );
}

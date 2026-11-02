import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { api, type GovernanceGatewaySummary } from "../../../lib/api";
import { useLoad } from "../../hooks";
import { Alert, Button, type Column, Modal, Table, Tag } from "../../ui";
import { StatusTag } from "./tags";

/** The gateway page that holds the import: its overview's Registry card (`focus=registry`). */
function gatewayImportUrl(gatewayId: string): string {
  return `/v2/governance?${new URLSearchParams({ view: "gateway", gateway: gatewayId, focus: "registry" })}`;
}

/**
 * The registry's 导入 Gateway: a Gateway's MCP record is previewed and imported from its
 * governance page (the Registry card there shows the record it would write and any name
 * conflict), so this picks the Gateway and opens that card rather than the gateway list.
 */
export function ImportGatewayDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const gateways = useLoad(() => (open ? api.listGovernanceGateways() : Promise.resolve(null)), `registry-import-gateways:${open}`);
  const rows = gateways.data?.gateways ?? [];

  const columns: Column<GovernanceGatewaySummary>[] = [
    {
      key: "name",
      title: t("v2.registry.importPicker.colGateway"),
      render: (gw) => (
        <>
          <b>{gw.name}</b>
          <span className="sub mono">{gw.id}</span>
        </>
      ),
    },
    {
      key: "record",
      title: t("v2.registry.importPicker.colRecord"),
      render: (gw) =>
        gw.registry_record ? <StatusTag status={gw.registry_record.status} /> : <Tag tone="outline">{t("v2.registry.importPicker.notImported")}</Tag>,
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (gw) => (
        <Button
          size="sm"
          kind={gw.registry_record ? undefined : "primary"}
          onClick={() => navigate(gatewayImportUrl(gw.id))}
          testId={`v2-registry-import-${gw.id}`}
        >
          {gw.registry_record ? t("v2.registry.importPicker.open") : t("v2.registry.importPicker.go")}
        </Button>
      ),
    },
  ];

  return (
    <Modal title={t("v2.registry.importPicker.title")} open={open} onClose={onClose} wide testId="v2-registry-import-dialog">
      <Alert>{t("v2.registry.importPicker.intro")}</Alert>
      <Table
        columns={columns}
        rows={rows}
        rowKey={(gw) => gw.id}
        loading={gateways.loading && !gateways.data}
        error={gateways.error}
        onRetry={gateways.reload}
        empty={t("v2.registry.importPicker.empty")}
        testId="v2-registry-import-gateways"
      />
    </Modal>
  );
}

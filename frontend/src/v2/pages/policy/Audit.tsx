import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { api, type PolicyChange, type PolicyEngine } from "../../../lib/api";
import { governanceError } from "../../../lib/governance";
import { fmtTime } from "../../format";
import { useLoad, usePaged } from "../../hooks";
import { Alert, Button, Card, type Column, Descriptions, Drawer, FilterSelect, LinkButton, Pager, Table, Tag } from "../../ui";
import { JsonBlock } from "../governance/widgets";
import { policyEditorHref } from "./common";

function ChangeDrawer({ engine, change, onClose }: { engine: PolicyEngine; change: PolicyChange; onClose: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <Drawer
      open
      title={
        <span className="v2-row">
          <span className="mono">{change.operation}</span>
          <Tag tone={change.status === "succeeded" ? "green" : "red"}>{t(`v2.governance.policy.changeStatus.${change.status}`)}</Tag>
        </span>
      }
      onClose={onClose}
      testId="v2-policy-audit-drawer"
      footer={
        <>
          <Button onClick={onClose} testId="v2-policy-audit-close">{t("v2.common.close")}</Button>
          {change.policy_id && (
            <Button kind="primary" onClick={() => navigate(policyEditorHref(engine.id, change.policy_id))} testId="v2-policy-audit-open-editor">
              {t("v2.governance.policy.openEditor")}
            </Button>
          )}
        </>
      }
    >
      {change.error && <Alert tone="error">{change.error}</Alert>}
      <Descriptions
        one
        items={[
          { label: t("v2.governance.policy.audit.time"), value: fmtTime(change.created_at) },
          { label: t("v2.governance.policy.audit.operator"), value: <span className="mono">{change.operator}</span> },
          { label: t("v2.governance.policy.colPolicy"), value: <span className="mono">{change.policy_id ?? "—"}</span> },
          { label: t("v2.governance.policy.colVersion"), value: change.version != null ? `v${change.version}` : "—" },
          { label: "ID", value: <span className="mono">{change.id}</span> },
        ]}
      />
      <div className="v2-sub-title">{t("v2.governance.policy.audit.before")}</div>
      <JsonBlock value={change.before} />
      <div className="v2-sub-title">{t("v2.governance.policy.audit.requested")}</div>
      <JsonBlock value={change.requested} />
      <div className="v2-sub-title">{t("v2.governance.policy.audit.after")}</div>
      <JsonBlock value={change.after} />
    </Drawer>
  );
}

/** 变更日志 of one engine (`GET /api/policy-engines/{id}/changes`): every create, publish,
 *  retire, delete, enforcement change and attachment — immutable, observability only. */
export function AuditTab({ engine }: { engine: PolicyEngine }) {
  const { t } = useTranslation();
  const { data, loading, error, reload } = useLoad(
    () => api.policyChanges(engine.id).catch((err: unknown) => Promise.reject(new Error(governanceError(err)))),
    `policy-changes:${engine.id}`,
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [op, setOp] = useState("");
  const changes = data?.changes ?? [];
  const rows = changes.filter((c) => (!status || c.status === status) && (!op || c.operation === op));
  const paged = usePaged(rows, 12);
  const selected = changes.find((c) => c.id === selectedId) ?? null;

  const columns: Column<PolicyChange>[] = [
    { key: "time", title: t("v2.governance.policy.audit.time"), className: "nowrap", render: (c) => fmtTime(c.created_at) },
    {
      key: "operation",
      title: t("v2.governance.policy.audit.operation"),
      render: (c) => (
        <LinkButton onClick={() => setSelectedId(c.id)} testId={`v2-policy-audit-${c.id}`}>
          <span className="mono">{c.operation}</span>
        </LinkButton>
      ),
    },
    {
      key: "resource",
      title: t("v2.governance.policy.audit.resource"),
      render: (c) => (
        <>
          <span className="mono">{c.policy_id ?? t("v2.governance.policy.audit.engineScope")}</span>
          {c.version != null && <span className="sub mono">v{c.version}</span>}
        </>
      ),
    },
    { key: "operator", title: t("v2.governance.policy.audit.operator"), render: (c) => <span className="mono">{c.operator}</span> },
    {
      key: "status",
      title: t("v2.governance.colStatus"),
      render: (c) => (
        <Tag tone={c.status === "succeeded" ? "green" : "red"} title={c.error ?? undefined}>
          {t(`v2.governance.policy.changeStatus.${c.status}`)}
        </Tag>
      ),
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (c) => <LinkButton onClick={() => setSelectedId(c.id)} testId={`v2-policy-audit-view-${c.id}`}>{t("v2.common.view")}</LinkButton>,
    },
  ];

  return (
    <Card title={t("v2.governance.policy.audit.title")} sub={t("v2.governance.policy.audit.sub", { name: engine.name })} testId="v2-policy-audit">
      <div className="v2-toolbar">
        <Button onClick={reload} testId="v2-policy-audit-refresh">{t("v2.common.refresh")}</Button>
        <FilterSelect
          label={t("v2.governance.policy.audit.operation")}
          value={op}
          allLabel={t("v2.common.all")}
          onChange={setOp}
          options={[...new Set(changes.map((c) => c.operation))].sort().map((value) => ({ value, label: value }))}
        />
        <FilterSelect
          label={t("v2.governance.colStatus")}
          value={status}
          allLabel={t("v2.common.all")}
          onChange={setStatus}
          options={["succeeded", "failed"].map((value) => ({ value, label: t(`v2.governance.policy.changeStatus.${value}`) }))}
        />
        <div className="end">
          <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
        </div>
      </div>
      <Table
        columns={columns}
        rows={paged.slice}
        rowKey={(c) => c.id}
        loading={loading}
        error={error}
        onRetry={reload}
        selectedKey={selectedId}
        empty={t("v2.governance.policy.audit.empty")}
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      {selected && <ChangeDrawer key={selected.id} engine={engine} change={selected} onClose={() => setSelectedId(null)} />}
    </Card>
  );
}

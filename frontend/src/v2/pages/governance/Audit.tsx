import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type GovernanceGatewayDetail, type GovernancePolicyChange } from "../../../lib/api";
import { governanceError } from "../../../lib/governance";
import { fmtTime } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  Descriptions,
  Drawer,
  Field,
  FilterSelect,
  LinkButton,
  Pager,
  Table,
} from "../../ui";
import { useGovernanceOperation } from "./common";
import { JsonBlock, OperationAlert, SharedEngineAck, StatusTag } from "./widgets";

/** A journal entry can be rolled back only from a policy snapshot (or a candidate it created). */
function hasRollbackSnapshot(change: GovernancePolicyChange): boolean {
  return (
    (change.status === "succeeded" || change.status === "partial") &&
    !!change.policy_id &&
    ("policy" in change.before || "policies" in change.before || !!change.candidate_policy_id)
  );
}

function AuditDrawer({
  gateway,
  change,
  onClose,
  onDone,
}: {
  gateway: GovernanceGatewayDetail;
  change: GovernancePolicyChange;
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [confirmationName, setConfirmationName] = useState("");
  const [sharedAcknowledged, setSharedAcknowledged] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const { operation, setOperation, pending } = useGovernanceOperation(onDone);
  const needsSharedAck = gateway.shared_gateways.length > 1;
  const snapshot = hasRollbackSnapshot(change);
  const ready =
    snapshot && confirmationName === gateway.name && (!needsSharedAck || sharedAcknowledged) && !pending && !busy;

  const rollback = async () => {
    if (!ready || !change.policy_id) return;
    setBusy(true);
    try {
      // the optimistic-concurrency tokens must be the live ones, not the page's snapshot
      const [liveGateway, livePolicies] = await Promise.all([
        api.getGovernanceGateway(gateway.id),
        api.listGovernancePolicies(gateway.id),
      ]);
      const livePolicy = livePolicies.policies.find((policy) => policy.id === change.policy_id);
      if (!livePolicy) throw new Error(t("governance.policyEditor.policyNotFound"));
      setOperation(
        await api.rollbackGovernancePolicy(liveGateway.id, change.policy_id, {
          expected_gateway_updated_at: liveGateway.updated_at,
          expected_policy_updated_at: livePolicy.updated_at,
          acknowledged_gateway_ids:
            liveGateway.shared_gateways.length > 1 ? liveGateway.shared_gateways.map((item) => item.id) : [],
          confirmation_name: confirmationName,
          evidence_range: "24h",
          audit_id: change.id,
        }),
      );
      toast("success", t("governance.messages.requestAccepted"));
    } catch (error) {
      toast("error", governanceError(error));
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <Drawer
      open
      title={
        <span className="v2-row">
          {change.operation} <StatusTag status={change.status} />
        </span>
      }
      onClose={onClose}
      testId="v2-governance-audit-drawer"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.close")}</Button>
          <Button kind="danger" disabled={!ready} onClick={() => setConfirming(true)} testId="v2-governance-audit-rollback">
            {t("v2.governance.rollback")}
          </Button>
        </>
      }
    >
      <OperationAlert operation={operation} />
      {change.error && <Alert tone="error">{change.error}</Alert>}
      <Descriptions
        one
        items={[
          { label: t("v2.governance.audit.time"), value: fmtTime(change.created_at) },
          { label: t("v2.governance.audit.operator"), value: <span className="mono">{change.operator}</span> },
          { label: "Gateway", value: change.gateway_name },
          { label: t("v2.governance.engineName"), value: <span className="mono">{change.engine_id ?? "—"}</span> },
          { label: t("v2.governance.colPolicy"), value: <span className="mono">{change.policy_id ?? "—"}</span> },
          { label: t("v2.governance.audit.candidate"), value: <span className="mono">{change.candidate_policy_id ?? "—"}</span> },
          { label: t("v2.governance.overrideReason"), value: change.override_reason ?? "—" },
          { label: "ID", value: <span className="mono">{change.id}</span> },
        ]}
      />
      <div className="v2-sub-title">{t("v2.governance.audit.before")}</div>
      <JsonBlock value={change.before} />
      <div className="v2-sub-title">{t("v2.governance.audit.requested")}</div>
      <JsonBlock value={change.requested} />
      <div className="v2-sub-title">{t("v2.governance.audit.after")}</div>
      <JsonBlock value={change.after} />

      <div className="v2-sub-title">{t("v2.governance.rollback")}</div>
      {!snapshot ? (
        <Alert tone="warn">{t("governance.audit.rollbackUnavailable")}</Alert>
      ) : (
        <div className="v2-form">
          {needsSharedAck && (
            <SharedEngineAck gateway={gateway} checked={sharedAcknowledged} onChange={setSharedAcknowledged} t={t} />
          )}
          <Field label={t("v2.governance.confirmName")} required hint={t("v2.governance.confirmNameHint", { name: gateway.name })}>
            <input
              className="v2-input mono"
              value={confirmationName}
              placeholder={gateway.name}
              onChange={(e) => setConfirmationName(e.target.value)}
            />
          </Field>
        </div>
      )}
      <Confirm
        open={confirming}
        title={t("v2.governance.confirmTitle.rollback")}
        body={t("governance.confirm.rollbackAudit", {
          operation: change.operation,
          gateway: gateway.name,
          policy: change.policy_id,
        })}
        confirmLabel={t("v2.governance.rollback")}
        danger
        busy={busy}
        onConfirm={() => void rollback()}
        onClose={() => setConfirming(false)}
      />
    </Drawer>
  );
}

/** Immutable journal of every governance change Launchpad made to this Gateway. */
export function AuditSection({ gateway, refreshTick }: { gateway: GovernanceGatewayDetail; refreshTick: number }) {
  const { t } = useTranslation();
  const [tick, setTick] = useState(0);
  const { data, loading, error, reload } = useLoad(
    () => api.governanceAudit(gateway.id).catch((err: unknown) => Promise.reject(new Error(governanceError(err)))),
    `gov-audit:${gateway.id}:${refreshTick}:${tick}`,
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [op, setOp] = useState("");
  const changes = data?.changes ?? [];
  const rows = changes.filter((c) => (!status || c.status === status) && (!op || c.operation === op));
  const paged = usePaged(rows, 12);
  const selected = changes.find((c) => c.id === selectedId) ?? null;
  const onDone = useCallback(() => setTick((n) => n + 1), []);

  const columns: Column<GovernancePolicyChange>[] = [
    { key: "time", title: t("v2.governance.audit.time"), className: "nowrap", render: (c) => fmtTime(c.created_at) },
    {
      key: "operation",
      title: t("v2.governance.audit.operation"),
      render: (c) => (
        <LinkButton onClick={() => setSelectedId(c.id)} testId={`v2-governance-audit-${c.id}`}>
          <span className="mono">{c.operation}</span>
        </LinkButton>
      ),
    },
    {
      key: "resource",
      title: t("v2.governance.audit.resource"),
      render: (c) => (
        <>
          <span className="mono ellipsis" style={{ maxWidth: 340 }} title={c.policy_id ?? c.engine_id ?? c.gateway_name}>
            {c.policy_id ?? c.engine_id ?? c.gateway_name}
          </span>
          <span className="sub mono">ID: {c.id}</span>
        </>
      ),
    },
    { key: "operator", title: t("v2.governance.audit.operator"), render: (c) => <span className="mono">{c.operator}</span> },
    {
      key: "status",
      title: t("v2.governance.colStatus"),
      render: (c) => <StatusTag status={c.status} title={c.error ?? undefined} />,
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (c) => (
        <div className="v2-actions">
          <LinkButton onClick={() => setSelectedId(c.id)}>{t("v2.common.view")}</LinkButton>
        </div>
      ),
    },
  ];

  return (
    <Card title={t("v2.governance.audit.title")} sub={t("governance.audit.immutable")} testId="v2-governance-audit">
      <div className="v2-toolbar">
        <Button onClick={reload}>{t("v2.common.refresh")}</Button>
        <FilterSelect
          label={t("v2.governance.audit.operation")}
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
          options={[...new Set(changes.map((c) => c.status))].sort().map((value) => ({ value, label: value }))}
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
        empty={t("v2.governance.audit.empty")}
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      {selected && (
        <AuditDrawer key={selected.id} gateway={gateway} change={selected} onClose={() => setSelectedId(null)} onDone={onDone} />
      )}
    </Card>
  );
}

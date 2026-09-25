import type { TFunction } from "i18next";

import type { GovernanceGatewayDetail, GovernanceOperation } from "../../../lib/api";
import { Alert, Tag } from "../../ui";
import { govTone } from "./common";

/** An AWS status / mode string as a toned tag; "—" when absent. */
export function StatusTag({ status, title }: { status: string | null | undefined; title?: string }) {
  if (!status) return <span className="v2-muted">—</span>;
  return (
    <Tag tone={govTone(status)} title={title}>
      {status}
    </Tag>
  );
}

/** Status line of the last governance operation of this page. */
export function OperationAlert({ operation }: { operation: GovernanceOperation | null }) {
  if (!operation) return null;
  const tone =
    operation.status === "succeeded"
      ? "success"
      : operation.status === "failed" || operation.status === "partial" || operation.status === "interrupted"
        ? "error"
        : "info";
  return (
    <Alert tone={tone}>
      <span className="v2-row" data-testid="v2-governance-operation">
        <StatusTag status={operation.status} />
        <b>{operation.operation}</b>
        <span className="mono v2-muted">{operation.id}</span>
        {operation.error && <span>{operation.error}</span>}
      </span>
    </Alert>
  );
}

/** Shared Policy Engine: every affected Gateway must be acknowledged before a mutation. */
export function SharedEngineAck({
  gateway,
  checked,
  onChange,
  t,
}: {
  gateway: GovernanceGatewayDetail;
  checked: boolean;
  onChange: (checked: boolean) => void;
  t: TFunction;
}) {
  return (
    <Alert tone="warn">
      <b>{t("v2.governance.sharedEngine")}</b>
      <ul className="v2-list">
        {gateway.shared_gateways.map((item) => (
          <li key={item.id}>
            {item.name} <span className="mono v2-muted">{item.id}</span>
          </li>
        ))}
      </ul>
      <label className="v2-check" style={{ marginTop: 6 }}>
        <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
        {t("governance.detail.sharedAck")}
      </label>
    </Alert>
  );
}

/** Pretty JSON block. */
export function JsonBlock({ value }: { value: unknown }) {
  return <pre className="v2-pre">{JSON.stringify(value ?? null, null, 2)}</pre>;
}

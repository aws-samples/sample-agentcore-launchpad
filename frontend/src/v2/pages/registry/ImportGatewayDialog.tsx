import { Trans, useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";

import type { GatewayImportHolder, GatewayImportResult, GatewayImportTarget } from "../../../lib/api";
import { memberApprovals, previewState, resultState, summarize, useGatewayImport } from "../../../lib/gateway-import";
import { Alert, Button, type Column, Select, Spin, Table, Tag } from "../../ui";
import { statusLabel } from "./common";
import { awsStatusLabel } from "../../awsStatus";

/** N17-obs250: who holds a target's name — the platform/another gateway's MCP record, or a
 *  member-registered MCP server. The holding record is a link to its detail when the host
 *  page can open one (`onOpenRecord`). */
function HeldBy({
  target,
  holder,
  onOpenRecord,
}: {
  target: string;
  holder: GatewayImportHolder;
  onOpenRecord?: (recordId: string) => void;
}) {
  const { t } = useTranslation();
  const recordId = holder.record_id;
  const link =
    recordId && onOpenRecord ? (
      // a bare element: <Trans> fills its children with the translated link text
      <button
        type="button"
        className="v2-link"
        onClick={() => onOpenRecord(recordId)}
        data-testid={`v2-import-gw-held-link-${target}`}
      />
    ) : (
      <b />
    );
  return (
    <span data-testid={`v2-import-gw-held-${target}`}>
      <Trans
        i18nKey={holder.origin === "gateway" ? "registry.importGw.heldBy.gateway" : "registry.importGw.heldBy.member"}
        values={{
          gateway: holder.gateway_name ?? holder.gateway_id ?? "—",
          name: holder.name ?? target,
          status: holder.status ? statusLabel(t, holder.status) : "—",
        }}
        components={{ rec: link }}
      />
    </span>
  );
}

/** Registry → "Import Gateway targets" (WB-005). Lists the onboarded Gateways (platform
 *  first), lets the user tick targets (all by default), runs
 *  `POST /api/registry/import-gateway` and shows one result row per target. `onClose(true)`
 *  after an import ran so the caller refreshes the list. */
export function ImportGatewayDialog({
  open,
  onClose,
  onOpenRecord,
}: {
  open: boolean;
  onClose: (imported: boolean) => void;
  /** open a registry record's detail (closes the dialog first) — the held-name link */
  onOpenRecord?: (recordId: string) => void;
}) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const c = useGatewayImport(open);
  if (!open) return null;
  const close = () => onClose(c.result !== null);
  const gateways = c.preview?.gateways ?? [];
  const total = c.gateway?.targets.length ?? 0;

  const targetColumns: Column<GatewayImportTarget>[] = [
    {
      key: "pick",
      title: "",
      width: 36,
      render: (row) => (
        <input
          type="checkbox"
          aria-label={row.target}
          disabled={!row.target_id || c.result !== null}
          checked={!!row.target_id && c.selected.has(row.target_id)}
          onChange={() => row.target_id && c.toggle(row.target_id)}
          data-testid={`v2-import-gw-pick-${row.target}`}
        />
      ),
    },
    {
      key: "target",
      title: t("registry.importGw.col.target"),
      render: (row) => (
        <>
          <b>{row.target}</b>
          {row.target_status && <span className="v2-muted"> · {awsStatusLabel(t, row.target_status)}</span>}
        </>
      ),
    },
    {
      // N17-obs261: what the target points at (from the inventory already listed)
      key: "endpoint",
      title: t("registry.importGw.col.endpoint"),
      render: (row) => (
        <span data-testid={`v2-import-gw-endpoint-${row.target}`}>
          {row.endpoint ? <code style={{ wordBreak: "break-all" }}>{row.endpoint}</code> : "—"}
          {row.endpoint_kind && <span className="v2-muted"> · {row.endpoint_kind}</span>}
        </span>
      ),
    },
    {
      key: "tools",
      title: t("registry.importGw.col.tools"),
      width: 120,
      render: (row) => (
        <>
          {row.tools} <span className="v2-muted">({t(`registry.importGw.toolSource.${row.tool_source}`)})</span>
        </>
      ),
    },
    {
      key: "record",
      title: t("registry.importGw.col.record"),
      render: (row) => {
        const state = previewState(row);
        if (state === "held" && row.held_by) {
          return (
            <Tag tone="red" title={row.record_id ?? undefined}>
              <HeldBy
                target={row.target}
                holder={row.held_by}
                onOpenRecord={
                  onOpenRecord &&
                  ((id: string) => {
                    onClose(c.result !== null);
                    onOpenRecord(id);
                  })
                }
              />
            </Tag>
          );
        }
        return (
          <span className="v2-row" style={{ gap: 4, flexWrap: "wrap" }}>
            <Tag tone={state === "held" ? "red" : state === "imported" ? "green" : "gray"} title={row.record_id ?? undefined}>
              {t(`registry.importGw.state.${state}`)}
              {row.status ? ` · ${statusLabel(t, row.status)}` : ""}
            </Tag>
            {/* N17-obs261: who put the record in its state, and what this import does to it */}
            {row.origin && (
              <Tag tone={row.origin === "member" ? "orange" : "outline"} testId={`v2-import-gw-origin-${row.target}`}>
                {t(`registry.importGw.origin.${row.origin}`)}
              </Tag>
            )}
            {row.will_approve && (
              <Tag tone="orange" testId={`v2-import-gw-will-approve-${row.target}`}>
                {t("registry.importGw.willApprove")}
              </Tag>
            )}
          </span>
        );
      },
    },
  ];
  // N17-obs261: the ticked member submissions an administrator's import would approve
  const approvingMembers = c.gateway && !c.result ? memberApprovals(c.gateway, c.selected) : 0;

  const resultColumns: Column<GatewayImportResult>[] = [
    { key: "target", title: t("registry.importGw.col.target"), render: (row) => <b>{row.target}</b> },
    {
      key: "result",
      title: t("registry.importGw.col.result"),
      render: (row) => {
        const state = resultState(row);
        const tone = state === "failed" ? "red" : state === "already" ? "gray" : "green";
        return (
          <div>
            <Tag tone={tone} testId={`v2-import-gw-result-${row.target}`}>
              {t(`registry.importGw.state.${state}`)}
              {row.status && state !== "reconciled" ? ` · ${statusLabel(t, row.status)}` : ""}
            </Tag>
            {row.error && <div className="v2-muted">{row.error}</div>}
            {row.auth_warning && !row.error && <div className="v2-muted">{row.auth_warning}</div>}
          </div>
        );
      },
    },
    {
      key: "record",
      title: t("registry.importGw.col.record"),
      render: (row) => <code>{row.record_id ?? "—"}</code>,
    },
  ];

  const summary = c.result ? summarize(c.result.results) : null;
  // N17-obs257: records this import created that wait for an administrator's decision
  const awaiting = c.result
    ? c.result.results.filter((r) => r.created && r.status === "PENDING_APPROVAL").length
    : 0;

  return (
    <div className="v2-mask" onMouseDown={(e) => e.target === e.currentTarget && close()}>
      <div className="v2-modal wide" role="dialog" aria-modal="true" data-testid="v2-import-gw">
        <div className="v2-modal-head">
          {c.result ? t("registry.importGw.resultsTitle", { gateway: c.result.gateway_name }) : t("registry.importGw.title")}
          <button type="button" onClick={close} aria-label={t("v2.common.close")}>
            ×
          </button>
        </div>
        <div className="v2-modal-body">
          {c.loading && <Spin />}
          {c.loadError && <Alert tone="error">{t("registry.importGw.loadFailed", { msg: c.loadError })}</Alert>}
          {!c.loading && !c.loadError && c.preview && gateways.length === 0 && (
            <Alert tone="warn">{t("registry.importGw.noGateways")}</Alert>
          )}
          {c.gateway && !c.result && (
            <>
              <p className="v2-muted">{t("registry.importGw.intro")}</p>
              <div className="v2-row" style={{ alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                <label htmlFor="v2-import-gw-gateway">{t("registry.importGw.gateway")}</label>
                <Select
                  id="v2-import-gw-gateway"
                  value={c.gatewayId}
                  onChange={c.pickGateway}
                  options={gateways.map((g) => ({ value: g.gateway_id, label: `${g.name} · ${g.gateway_id}` }))}
                  testId="v2-import-gw-gateway"
                />
                {c.gateway.platform && <Tag tone="blue">{t("registry.importGw.platformTag")}</Tag>}
                <Tag tone={c.gateway.auth_warning ? "orange" : "outline"}>
                  {t("registry.importGw.authTag", { auth: c.gateway.auth })}
                </Tag>
              </div>
              <Alert tone="info">
                {t(
                  !c.gateway.platform
                    ? "registry.importGw.memberNote"
                    : isAdmin
                      ? "registry.importGw.platformNote"
                      : "registry.importGw.platformNoteMember", // N17-obs257
                )}
              </Alert>
              {c.gateway.auth_warning && <Alert tone="warn">{c.gateway.auth_warning}</Alert>}
              {c.gateway.live_error && (
                <Alert tone="warn">{t("registry.importGw.liveError", { error: c.gateway.live_error })}</Alert>
              )}
              {total === 0 ? (
                <Alert tone="warn">{t("registry.importGw.noTargets")}</Alert>
              ) : (
                <>
                  <div className="v2-row" style={{ alignItems: "center", gap: 8 }}>
                    <span className="v2-muted" data-testid="v2-import-gw-selected">
                      {t("registry.importGw.selected", { count: c.selected.size, total })}
                    </span>
                    <Button size="sm" onClick={c.selectAll}>{t("registry.importGw.selectAll")}</Button>
                    <Button size="sm" onClick={c.selectNone}>{t("registry.importGw.selectNone")}</Button>
                  </div>
                  {approvingMembers > 0 && (
                    <Alert tone="warn">
                      <span data-testid="v2-import-gw-member-approvals">
                        {t("registry.importGw.memberApprovals", { count: approvingMembers })}
                      </span>
                    </Alert>
                  )}
                  <Table columns={targetColumns} rows={c.gateway.targets} rowKey={(row) => row.target_id ?? row.target} />
                </>
              )}
              {c.importError && <Alert tone="error">{t("registry.importGw.importFailed", { msg: c.importError })}</Alert>}
            </>
          )}
          {c.result && summary && (
            <>
              <Alert tone={summary.failed ? "warn" : "success"}>
                <span data-testid="v2-import-gw-summary">{t("registry.importGw.summary", { ...summary })}</span>
              </Alert>
              {awaiting > 0 && (
                <Alert>
                  <span data-testid="v2-import-gw-awaiting">{t("registry.importGw.awaitingAdmin", { count: awaiting })}</span>
                </Alert>
              )}
              {c.result.live_error && (
                <Alert tone="warn">{t("registry.importGw.liveError", { error: c.result.live_error })}</Alert>
              )}
              <Table columns={resultColumns} rows={c.result.results} rowKey={(row) => row.target_id ?? row.target} />
            </>
          )}
        </div>
        <div className="v2-modal-foot">
          {c.result ? (
            <Button kind="primary" onClick={close} testId="v2-import-gw-done">{t("registry.importGw.done")}</Button>
          ) : (
            <>
              <Button onClick={close}>{t("v2.common.cancel")}</Button>
              <Button
                kind="primary"
                disabled={c.importing || !c.gateway || c.selected.size === 0}
                onClick={() => void c.run()}
                testId="v2-import-gw-confirm"
              >
                {c.importing ? t("registry.importGw.importing") : t("registry.importGw.confirm", { count: c.selected.size })}
              </Button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

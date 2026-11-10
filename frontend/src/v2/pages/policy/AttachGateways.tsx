import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, type GatewayPolicyEngine, type GovernanceGatewaySummary, type PolicyEngine } from "../../../lib/api";
import { governanceError } from "../../../lib/governance";
import { fmtTime } from "../../format";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, type Column, Confirm, LinkButton, Table, Tag } from "../../ui";
import { StatusTag } from "../governance/widgets";
import { AttachEngineDialog } from "./AttachEngineDialog";
import { detachEngine } from "./attachOps";
import { gatewayPolicyHref, gatewayRegion } from "./common";
import { PolicyModeTag } from "./widgets";


interface Row {
  gateway: GovernanceGatewaySummary;
  region: string | null;
  /** the gateway lives in another region than the engine (the workspace's) — the backend
   *  refuses the attach with 422 `gateway.policy_engine_region` */
  crossRegion: boolean;
  /** ruling 24: governed by another environment's hub — nothing can be changed from here */
  foreign: boolean;
  foreignLambda: string | null;
  /** the engine attached to this gateway (its `policy-engine` read, else every engine's
   *  `attachments[]`), if any */
  engine: PolicyEngine | null;
  mode: string | null;
  attachedAt: string | null;
}

/**
 * 附加到网关 — every gateway of the account with the engine it carries and the mode. One
 * engine per gateway, many gateways per engine (Global): "attach this engine" on a free gateway,
 * "change mode" / "detach" on one that carries THIS engine, "replace" on one that carries
 * another. The joins read the engine list (`attachments[]`) so no per-gateway call is needed.
 */
export function AttachGatewaysTab({ engine, onChanged }: { engine: PolicyEngine; onChanged: () => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { isAdmin } = useAuth();
  const { current } = useWorkspace();
  const homeRegion = current?.region ?? null;
  const gateways = useLoad(
    () => api.listGovernanceGateways(false, {
      workspaceId: current?.id, includeWorkspaceRegions: true,
    }).catch((err: unknown) => Promise.reject(new Error(governanceError(err)))),
    `policy-gateways:${engine.id}`,
  );
  const engines = useLoad(
    () => api.listPolicyEngines().catch((err: unknown) => Promise.reject(new Error(governanceError(err)))),
    `policy-engines:${engine.id}:${engine.attachments.length}`,
  );
  // every gateway's own attachment read: names a foreign hub's attachment the engine list cannot
  const attachments = useLoad(
    async () => {
      // A cross-region row is display-only; querying its attachment in the engine's
      // region would produce the expected 422 instead of useful attachment data.
      const list = (gateways.data?.gateways ?? []).filter((g) =>
        !homeRegion || gatewayRegion(g) === homeRegion,
      );
      const results = await Promise.allSettled(list.map((g) => api.gatewayPolicyEngine(g.id)));
      const out: Record<string, GatewayPolicyEngine> = {};
      results.forEach((r, i) => {
        if (r.status === "fulfilled") out[list[i].id] = r.value;
      });
      return out;
    },
    `policy-gateway-attachments:${engine.id}:${(gateways.data?.gateways ?? []).map((g) => g.id).join(",")}`,
  );
  const [attach, setAttach] = useState<Row | null>(null);
  const [detach, setDetach] = useState<Row | null>(null);
  const [busy, setBusy] = useState(false);

  const rows: Row[] = useMemo(() => {
    const all = engines.data?.engines ?? [engine];
    return (gateways.data?.gateways ?? []).map((gateway) => {
      const region = gatewayRegion(gateway);
      const crossRegion = !!homeRegion && !!region && region !== homeRegion;
      const own = attachments.data?.[gateway.id];
      const foreign = own?.foreign === true;
      const base = { gateway, region, crossRegion, foreign, foreignLambda: own?.foreign_lambda ?? null };
      if (own?.attached) {
        const e = all.find((x) => x.id === own.engine_id) ?? null;
        // a foreign hub's engine is not in this workspace's list: name it from the read
        const named = e ?? (own.engine_id ? ({ ...engine, id: own.engine_id, name: own.engine_name ?? own.engine_id, attachments: [], policy_count: 0 } as PolicyEngine) : null);
        return { ...base, engine: named, mode: own.mode, attachedAt: own.attached_at ?? null };
      }
      for (const e of all) {
        const a = e.attachments.find((x) => x.gateway_id === gateway.id);
        if (a) return { ...base, engine: e, mode: a.mode, attachedAt: a.attached_at ?? null };
      }
      return { ...base, engine: null, mode: null, attachedAt: null };
    });
  }, [attachments.data, engine, engines.data, gateways.data, homeRegion]);
  const mine = rows.filter((r) => r.engine?.id === engine.id).length;

  const reloadAll = () => {
    gateways.reload();
    engines.reload();
    attachments.reload();
    onChanged();
  };

  const runDetach = async () => {
    if (!detach) return;
    setBusy(true);
    const next = await detachEngine(detach.gateway.id, t, toast, detach.gateway.name);
    setBusy(false);
    setDetach(null);
    if (next) reloadAll();
  };

  const columns: Column<Row>[] = [
    {
      key: "gateway",
      title: t("v2.governance.colGateway"),
      render: (r) => (
        <>
          {r.crossRegion ? (
            <span data-testid={`v2-policy-gw-${r.gateway.id}`}>{r.gateway.name}</span>
          ) : (
            <Link className="v2-governance-link" to={gatewayPolicyHref(r.gateway.id)} data-testid={`v2-policy-gw-${r.gateway.id}`}>
              {r.gateway.name}
            </Link>
          )}
          <span className="sub mono">ID: {r.gateway.id}</span>
        </>
      ),
    },
    { key: "status", title: t("v2.governance.colStatus"), render: (r) => <StatusTag status={r.gateway.status} /> },
    {
      key: "region",
      title: t("v2.policy.colRegion"),
      render: (r) => (
        <span data-testid={`v2-policy-gw-region-${r.gateway.id}`}>
          <span className="mono">{r.region ?? "—"}</span>
          {r.crossRegion && (
            <Tag tone="gray" title={t("apiErrors.gateway.policy_engine_region")} testId={`v2-policy-gw-cross-region-${r.gateway.id}`}>
              {t("v2.policy.crossRegion")}
            </Tag>
          )}
        </span>
      ),
    },
    {
      key: "engine",
      title: t("v2.policy.colAttachedEngine"),
      render: (r) =>
        r.engine ? (
          <span data-testid={`v2-policy-gw-engine-${r.gateway.id}`}>
            <span className="mono">{r.engine.name}</span>
            {r.engine.id === engine.id && !r.foreign && <Tag tone="blue">{t("v2.policy.thisEngine")}</Tag>}
            {r.foreign && (
              <Tag tone="gray" title={r.foreignLambda ?? undefined} testId={`v2-policy-gw-foreign-${r.gateway.id}`}>
                {t("v2.policy.foreign")}
              </Tag>
            )}
          </span>
        ) : r.foreign ? (
          <Tag tone="gray" title={r.foreignLambda ?? undefined} testId={`v2-policy-gw-foreign-${r.gateway.id}`}>
            {t("v2.policy.foreign")}
          </Tag>
        ) : (
          <span className="v2-muted" data-testid={`v2-policy-gw-engine-${r.gateway.id}`}>
            {t("v2.policy.notAttached")}
          </span>
        ),
    },
    { key: "mode", title: t("v2.policy.attach.mode"), render: (r) => <PolicyModeTag mode={r.mode} testId={`v2-policy-gw-mode-${r.gateway.id}`} /> },
    { key: "since", title: t("v2.policy.attachedAt"), className: "nowrap", render: (r) => (r.attachedAt ? fmtTime(r.attachedAt) : "—") },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (r) => (
        <div className="v2-actions">
          {r.foreign ? (
            <span className="v2-muted" title={r.foreignLambda ?? undefined} data-testid={`v2-policy-gw-foreign-actions-${r.gateway.id}`}>
              {t("v2.policy.foreignActions")}
            </span>
          ) : r.engine?.id === engine.id ? (
            <>
              <LinkButton disabled={!isAdmin || busy} onClick={() => setAttach(r)} testId={`v2-policy-gw-change-${r.gateway.id}`}>
                {t("v2.policy.attach.changeMode")}
              </LinkButton>
              <LinkButton danger disabled={!isAdmin || busy} onClick={() => setDetach(r)} testId={`v2-policy-gw-detach-${r.gateway.id}`}>
                {t("v2.policy.attach.detach")}
              </LinkButton>
            </>
          ) : (
            <LinkButton
              disabled={!isAdmin || busy || r.crossRegion}
              title={r.crossRegion ? t("apiErrors.gateway.policy_engine_region") : undefined}
              onClick={() => setAttach(r)}
              testId={`v2-policy-gw-attach-${r.gateway.id}`}
            >
              {t(r.engine ? "v2.policy.attach.replace" : "v2.policy.attach.attachThis")}
            </LinkButton>
          )}
        </div>
      ),
    },
  ];

  return (
    <Card title={t("v2.policy.tab.gateways")} sub={t("v2.policy.gatewaysSub", { name: engine.name })} testId="v2-policy-gateways">
      <div className="v2-toolbar">
        <Button onClick={reloadAll} testId="v2-policy-gateways-refresh">{t("v2.common.refresh")}</Button>
        <div className="end">
          <span className="v2-count">{t("v2.policy.attachedCount", { count: mine })}</span>
        </div>
      </div>
      {!isAdmin && <Alert>{t("v2.policy.attach.adminOnly")}</Alert>}
      {engines.error && <Alert tone="warn">{engines.error}</Alert>}
      <Table
        columns={columns}
        rows={rows}
        rowKey={(r) => r.gateway.id}
        loading={gateways.loading}
        error={gateways.error}
        onRetry={reloadAll}
        empty={t("v2.governance.empty")}
        testId="v2-policy-gateway-table"
      />
      {attach && (
        <AttachEngineDialog
          open
          gatewayId={attach.gateway.id}
          gatewayName={attach.gateway.name}
          engineId={engine.id}
          current={
            attach.engine
              ? { attached: true, engine_id: attach.engine.id, engine_name: attach.engine.name, mode: attach.mode as "LOG_ONLY" | "ENFORCE", attached_at: attach.attachedAt }
              : { attached: false, engine_id: null, engine_name: null, mode: null }
          }
          onClose={() => setAttach(null)}
          onDone={() => {
            setAttach(null);
            reloadAll();
          }}
        />
      )}
      <Confirm
        open={detach !== null}
        title={t("v2.policy.attach.detachTitle")}
        body={t("v2.policy.attach.detachBody", { gateway: detach?.gateway.name ?? "", engine: engine.name })}
        confirmLabel={t("v2.policy.attach.detach")}
        danger
        busy={busy}
        onConfirm={() => void runDetach()}
        onClose={() => setDetach(null)}
      />
    </Card>
  );
}

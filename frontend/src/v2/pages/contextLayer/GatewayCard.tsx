import { RefreshCw, Trash2, Wrench } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, ApiError, type ContextLayerStatus, errorMessage } from "../../../lib/api";
import { SIGNAL_TONE, type Signal } from "../../signals";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, Descriptions, Lamp, Spin, Tag } from "../../ui";
import { CoaErrorNotice } from "./common";
import { gatewayActions, gatewaySignal } from "./state";
import type { CoaError } from "./useCoaLoad";

type Busy = "enable" | "refresh" | "delete" | null;

function Flag({ value, testId }: { value: boolean | null; testId: string }) {
  const { t } = useTranslation();
  const signal: Signal = value === null ? "info" : value ? "ok" : "act";
  return (
    <span className="v2-row" data-testid={testId} data-signal={signal}>
      <Lamp signal={signal} />
      {value === null ? t("v2.contextLayer.health.unknown") : t(value ? "v2.common.yes" : "v2.common.no")}
    </span>
  );
}

/**
 * The managed COA gateway (gateway mode): one AgentCore Gateway per workspace that
 * gateway-mode agents reach COA through, as the asking member. Read back from AWS
 * for everyone; administrators enable, repair, refresh its COA tools and delete it.
 * Consents given through it cannot be revoked on the platform, which the card says.
 */
export function GatewayCard({ layer, onChanged }: { layer: ContextLayerStatus; onChanged: () => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { isAdmin } = useAuth();
  const gw = useLoad(() => api.getContextLayerGateway(), `context-layer-gateway:${layer.gateway?.gateway_id ?? ""}`);
  const [busy, setBusy] = useState<Busy>(null);
  const [confirm, setConfirm] = useState(false);
  const [failure, setFailure] = useState<CoaError | null>(null);
  const [refused, setRefused] = useState<string[] | null>(null);
  const data = gw.data;
  const actions = gatewayActions(data, isAdmin);

  const run = async (kind: Exclude<Busy, null>, call: () => Promise<unknown>, done: string) => {
    setBusy(kind);
    setFailure(null);
    setRefused(null);
    try {
      await call();
      toast("success", t(done));
      setConfirm(false);
      gw.reload();
      onChanged();
    } catch (err) {
      const refs =
        err instanceof ApiError && err.code === "context_layer.gateway_referenced"
          ? ((err.detail as { referenced_by?: { name: string | null }[] } | null)?.referenced_by ?? [])
          : null;
      if (refs) setRefused(refs.map((r) => r.name ?? t("v2.contextLayer.hiddenAgent")));
      else setFailure({ code: err instanceof ApiError ? err.code : "network", message: errorMessage(err) });
      setConfirm(false);
      // a failed enable can still have created the gateway (kept, in a failed state)
      gw.reload();
      onChanged();
    } finally {
      setBusy(null);
    }
  };
  const enable = () => void run("enable", () => api.enableContextLayerGateway(), "v2.contextLayer.gateway.enabledDone");

  const signal = data ? gatewaySignal(data) : null;
  return (
    <Card
      title={t("v2.contextLayer.gateway.title")}
      sub={t("v2.contextLayer.gateway.sub")}
      testId="context-layer-gateway"
      end={
        data && signal ? (
          <span data-testid="context-layer-gateway-state" data-state={data.state} data-usable={data.usable ? "true" : "false"}>
            <Tag tone={SIGNAL_TONE[signal]} dot={data.state === "creating"}>
              {t(`v2.contextLayer.gateway.state.${data.state}`)}
            </Tag>
          </span>
        ) : undefined
      }
    >
      <div className="v2-cl-gateway">
        {gw.loading && !data && <Spin />}
        {gw.error && <Alert tone="error">{gw.error}</Alert>}
        {data && !data.enabled && (
          <p className="v2-muted" data-testid="context-layer-gateway-intro">
            {t("v2.contextLayer.gateway.intro")} {!isAdmin && t("v2.contextLayer.gateway.askAdmin")}
          </p>
        )}
        {data?.enabled && (
          <Descriptions
            items={[
              { label: t("v2.contextLayer.gateway.gatewayId"), value: <span className="mono">{data.gateway_id ?? "—"}</span> },
              {
                label: t("v2.contextLayer.gateway.url"),
                value: (
                  <span className="mono" data-testid="context-layer-gateway-url">
                    {data.gateway_url ?? "—"}
                  </span>
                ),
              },
              {
                label: t("v2.contextLayer.gateway.awsStatus"),
                value: (
                  <span className="mono">
                    {data.gateway_status ?? "—"} / {data.target_status ?? "—"}
                  </span>
                ),
              },
              {
                label: t("v2.contextLayer.gateway.versions"),
                value: <span className="mono">{data.protocol_versions.length ? data.protocol_versions.join(", ") : "—"}</span>,
              },
              {
                label: t("v2.contextLayer.gateway.tools", { n: data.tools.length }),
                value: data.tools.length ? (
                  <div className="v2-tags" data-testid="context-layer-gateway-tools" data-count={data.tools.length}>
                    {data.tools.map((name) => (
                      <Tag key={name} tone="outline">
                        <span className="mono">{name}</span>
                      </Tag>
                    ))}
                  </div>
                ) : (
                  <span data-testid="context-layer-gateway-tools" data-count={0}>
                    —
                  </span>
                ),
              },
              {
                label: t("v2.contextLayer.gateway.returnUrl"),
                value: <Flag value={data.return_url_ok} testId="context-layer-gateway-return-url" />,
              },
              {
                label: t("v2.contextLayer.gateway.agents"),
                value: data.agents.length ? (
                  <div className="v2-tags" data-testid="context-layer-gateway-agents">
                    {data.agents.map((ref, i) =>
                      ref.id ? (
                        <Link key={ref.id} to={`/v2/agents?view=detail&id=${encodeURIComponent(ref.id)}`}>
                          <Tag tone="outline">{ref.name}</Tag>
                        </Link>
                      ) : (
                        <Tag key={`hidden-${i}`} tone="gray">
                          {t("v2.contextLayer.hiddenAgent")}
                        </Tag>
                      ),
                    )}
                  </div>
                ) : (
                  t("v2.contextLayer.gateway.noAgents")
                ),
              },
            ]}
          />
        )}
        {data?.enabled && data.issues.length > 0 && (
          <Alert tone="warn">
            <span data-testid="context-layer-gateway-issues">{t("v2.contextLayer.gateway.issuesTitle")}</span>
            <ul className="v2-cl-list">
              {data.issues.map((issue) => (
                <li key={issue}>{t(`v2.contextLayer.gateway.issue.${issue}`)}</li>
              ))}
            </ul>
          </Alert>
        )}
        {data?.enabled && data.status_reasons.length > 0 && (
          <Alert tone="warn">
            {t("v2.contextLayer.gateway.reasons")}
            <ul className="v2-cl-list mono">
              {data.status_reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          </Alert>
        )}
        <Alert>{t("v2.contextLayer.gateway.revokeNote")}</Alert>
        {busy === "enable" && <Spin label={t("v2.contextLayer.gateway.busyEnable")} />}
        {failure && <CoaErrorNotice error={failure} onAuthorized={onChanged} testId="context-layer-gateway-error" />}
        {refused && <Alert tone="error">{t("v2.contextLayer.gateway.refused", { agents: refused.join(", ") })}</Alert>}
        {(actions.enable || actions.repair || actions.refresh || actions.remove) && (
          <div className="v2-row">
            {(actions.enable || actions.repair) && (
              <Button size="sm" kind="primary" disabled={busy !== null} onClick={enable} testId="context-layer-gateway-enable">
                {actions.repair && <Wrench size={13} aria-hidden="true" />}
                {t(actions.repair ? "v2.contextLayer.gateway.repair" : "v2.contextLayer.gateway.enable")}
              </Button>
            )}
            {actions.refresh && (
              <Button
                size="sm"
                disabled={busy !== null}
                onClick={() =>
                  void run("refresh", () => api.refreshContextLayerGatewayTools(), "v2.contextLayer.gateway.refreshedDone")
                }
                testId="context-layer-gateway-refresh"
              >
                <RefreshCw size={13} aria-hidden="true" />
                {t("v2.contextLayer.gateway.refresh")}
              </Button>
            )}
            {actions.remove && (
              <Button size="sm" kind="danger" disabled={busy !== null} onClick={() => setConfirm(true)} testId="context-layer-gateway-delete">
                <Trash2 size={13} aria-hidden="true" />
                {t("v2.contextLayer.gateway.delete")}
              </Button>
            )}
          </div>
        )}
      </div>
      <Confirm
        open={confirm}
        title={t("v2.contextLayer.gateway.deleteTitle")}
        body={t("v2.contextLayer.gateway.deleteBody", { gateway: data?.gateway_id ?? "" })}
        confirmLabel={t("v2.contextLayer.gateway.delete")}
        danger
        busy={busy === "delete"}
        onConfirm={() => void run("delete", () => api.disableContextLayerGateway(), "v2.contextLayer.gateway.deletedDone")}
        onClose={() => busy !== "delete" && setConfirm(false)}
      />
    </Card>
  );
}

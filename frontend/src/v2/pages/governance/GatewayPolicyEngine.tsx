import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, type GatewayPolicyEngine, type GovernanceGatewayDetail } from "../../../lib/api";
import { governanceError } from "../../../lib/governance";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, Descriptions, Tag } from "../../ui";
import { AttachEngineDialog } from "../policy/AttachEngineDialog";
import { detachEngine } from "../policy/attachOps";
import { engineHref, POLICY_PATH } from "../policy/common";
import { PolicyModeTag } from "../policy/widgets";

/**
 * 策略引擎 section of one Gateway (Global's `policyEngineConfiguration` on the gateway):
 * the attached engine and its mode, with 附加 / 更换 / 移除 — the same
 * `PUT` / `DELETE /api/gateways/{gid}/policy-engine` the engine page uses. At most one engine
 * per gateway; a gateway without an attachment is unguarded (nothing decides its tool calls).
 */
export function GatewayPolicyEngineSection({
  gateway,
  current,
  refreshTick,
  onChanged,
}: {
  gateway: GovernanceGatewayDetail;
  current: GatewayPolicyEngine | null;
  refreshTick: number;
  onChanged: (next: GatewayPolicyEngine) => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { isAdmin } = useAuth();
  const engines = useLoad(
    () => api.listPolicyEngines().catch((err: unknown) => Promise.reject(new Error(governanceError(err)))),
    `gateway-policy-engines:${gateway.id}:${refreshTick}`,
  );
  const attachment = useLoad(
    () => api.gatewayPolicyEngine(gateway.id).catch((err: unknown) => Promise.reject(new Error(governanceError(err)))),
    `gateway-policy-engine:${gateway.id}:${refreshTick}:${current?.engine_id ?? ""}:${current?.mode ?? ""}`,
  );
  // the server's answer to the last attach / detach wins until the next read lands
  const [override, setOverride] = useState<GatewayPolicyEngine | null>(null);
  useEffect(() => setOverride(null), [attachment.data]);
  const state = override ?? attachment.data ?? current;
  const [attachOpen, setAttachOpen] = useState(false);
  const [detachOpen, setDetachOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const runDetach = async () => {
    setBusy(true);
    const next = await detachEngine(gateway.id, t, toast, gateway.name);
    setBusy(false);
    setDetachOpen(false);
    if (next) {
      setOverride(next);
      onChanged(next);
      attachment.reload();
    }
  };

  const attached = state?.attached === true;
  // ruling 24: another environment's hub governs this gateway — read-only here
  const foreign = state?.foreign === true;
  return (
    <Card
      title={t("v2.policy.gateway.title")}
      sub={t("v2.policy.gateway.sub")}
      testId="v2-governance-policy-engine"
      end={
        <div className="v2-row">
          {attached ? (
            <>
              <Button size="sm" disabled={!isAdmin || busy || foreign} onClick={() => setAttachOpen(true)} testId="v2-governance-policy-engine-change">
                {t("v2.policy.attach.change")}
              </Button>
              <Button size="sm" kind="danger" disabled={!isAdmin || busy || foreign} onClick={() => setDetachOpen(true)} testId="v2-governance-policy-engine-detach">
                {t("v2.policy.attach.detach")}
              </Button>
            </>
          ) : (
            <Button size="sm" kind="primary" disabled={!isAdmin || busy || attachment.loading || foreign} onClick={() => setAttachOpen(true)} testId="v2-governance-policy-engine-attach">
              {t("v2.policy.attach.attach")}
            </Button>
          )}
        </div>
      }
    >
      {attachment.error && <Alert tone="error">{attachment.error}</Alert>}
      {!isAdmin && <Alert>{t("v2.policy.attach.adminOnly")}</Alert>}
      {foreign && (
        <Alert tone="warn" testId="v2-governance-policy-engine-foreign">
          <b>{t("v2.policy.foreign")}</b> {t("v2.policy.gateway.foreignNote")}
          {state?.foreign_lambda && (
            <>
              {" "}
              <span className="mono">{state.foreign_lambda}</span>
            </>
          )}
        </Alert>
      )}
      {state && !attached && !foreign && <Alert tone="warn">{t("v2.policy.gateway.unguarded")}</Alert>}
      {state && (
        <Descriptions
          items={[
            {
              label: t("v2.policy.engine"),
              value: attached && state.engine_id && foreign ? (
                // a foreign hub's engine has no page in this console
                <span className="mono" data-testid="v2-governance-policy-engine-name">{state.engine_name ?? state.engine_id}</span>
              ) : attached && state.engine_id ? (
                <Link className="v2-governance-link" to={engineHref(state.engine_id)} data-testid="v2-governance-policy-engine-name">
                  {state.engine_name ?? state.engine_id}
                </Link>
              ) : (
                <span className="v2-muted" data-testid="v2-governance-policy-engine-name">{t("v2.policy.notAttached")}</span>
              ),
            },
            { label: t("v2.policy.attach.mode"), value: <PolicyModeTag mode={state.mode} testId="v2-governance-policy-engine-mode" /> },
            { label: t("v2.policy.attachedAt"), value: state.attached_at ? fmtTime(state.attached_at) : "—" },
            {
              label: t("v2.policy.gateway.interceptor"),
              value: state.interceptor ? (
                <span data-testid="v2-governance-policy-interceptor">
                  <Tag tone={state.interceptor.attached ? "green" : "gray"} dot>
                    {t(state.interceptor.attached ? "v2.policy.gateway.interceptorAttached" : "v2.policy.gateway.interceptorDetached")}
                  </Tag>
                  {state.interceptor.bundle_version != null && <span className="sub mono"> {t("v2.policy.gateway.bundle", { version: state.interceptor.bundle_version })}</span>}
                  {state.interceptor.lambda_digest && <span className="sub mono"> {state.interceptor.lambda_digest.slice(0, 12)}</span>}
                </span>
              ) : (
                <span className="v2-muted">—</span>
              ),
            },
          ]}
        />
      )}
      {engines.data && engines.data.engines.length === 0 && (
        <div style={{ marginTop: 12 }}>
          <Alert action={<Link className="v2-link" to={POLICY_PATH} data-testid="v2-governance-policy-engine-open">{t("v2.policy.openEngines")}</Link>}>{t("v2.policy.gateway.noEngines")}</Alert>
        </div>
      )}
      <AttachEngineDialog
        open={attachOpen}
        gatewayId={gateway.id}
        gatewayName={gateway.name}
        current={state}
        engines={engines.data?.engines ?? []}
        onClose={() => setAttachOpen(false)}
        onDone={(next) => {
          setAttachOpen(false);
          setOverride(next);
          onChanged(next);
          attachment.reload();
          engines.reload();
        }}
      />
      <Confirm
        open={detachOpen}
        title={t("v2.policy.attach.detachTitle")}
        body={t("v2.policy.attach.detachBody", { gateway: gateway.name, engine: state?.engine_name ?? state?.engine_id ?? "" })}
        confirmLabel={t("v2.policy.attach.detach")}
        danger
        busy={busy}
        onConfirm={() => void runDetach()}
        onClose={() => setDetachOpen(false)}
      />
    </Card>
  );
}

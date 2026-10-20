import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  type AgentForm,
  type ContextLayerVia,
  contextLayerVia,
  NAMESPACE_ID_RE,
  withContextVia,
} from "../../../lib/agent-spec";
import { api, type ContextLayerNamespace, type ContextLayerStatus } from "../../../lib/api";
import { useLoad } from "../../hooks";
import { Alert, Button, Card, Field, Segmented, Select, Spin } from "../../ui";
import { AuthorizationControl } from "../contextLayer/AuthorizationControl";
import { authStatusOf, namespaceOptions, pickerMode, pickNamespace } from "../contextLayer/state";
import type { SectionProps } from "./wizardKit";

/**
 * 上下文层 (Context layer) in the agent wizard: attach one namespace of the
 * workspace's COA deployment. The agent then calls COA's tools as whoever chats with
 * it. The picker lists what COA lets the signed-in member see (it needs their
 * authorization); without it, a namespace id can be typed in. 接入方式 picks direct
 * (the default) or the workspace's managed COA gateway, offered only while an admin
 * has it enabled (`GET /api/context-layer` → `gateway.enabled`).
 */
export function ContextLayerSection({
  form,
  set,
  status,
  onStatusChanged,
  touched,
}: Pick<SectionProps, "form" | "set"> & {
  status: ContextLayerStatus | null;
  onStatusChanged: () => void;
  touched: boolean;
}) {
  const { t } = useTranslation();
  const auth = authStatusOf(status);
  const authorized = auth === "authorized";
  const namespaces = useLoad(
    () => (authorized ? api.listContextLayerNamespaces() : Promise.resolve({ namespaces: [] as ContextLayerNamespace[] })),
    `wizard-context-namespaces:${authorized}`,
  );
  const list = namespaces.data?.namespaces ?? null;
  const mode = pickerMode(auth, { data: list, error: namespaces.error });
  const current = form.contextLayer ?? null;
  const via = contextLayerVia(current);
  const gatewayEnabled = !!status?.gateway?.enabled;
  const setChoice = (next: AgentForm["contextLayer"]) => set({ contextLayer: next });
  // a new namespace keeps the access mode already chosen
  const setNamespace = (next: AgentForm["contextLayer"]) => setChoice(withContextVia(next ?? null, via));
  const badId = touched && !!current?.namespaceId.trim() && !NAMESPACE_ID_RE.test(current.namespaceId.trim());
  const note =
    via === "gateway"
      ? "v2.contextLayer.wizard.noteGateway"
      : form.method === "harness"
        ? "v2.contextLayer.wizard.noteHarness"
        : "v2.contextLayer.wizard.noteStrands";

  return (
    <Card title={t("v2.contextLayer.wizard.title")} sub={t("v2.contextLayer.wizard.sub")} testId="v2-agent-context-layer">
      {!status?.connected ? (
        <>
          <Alert tone="warn">{t("v2.contextLayer.wizard.notConnected")}</Alert>
          {current && (
            <div className="v2-row">
              <span className="mono">{current.namespaceName || current.namespaceId}</span>
              <Button size="sm" onClick={() => setChoice(null)} testId="v2-agent-context-remove">
                {t("v2.contextLayer.wizard.remove")}
              </Button>
            </div>
          )}
        </>
      ) : (
        <div className="v2-form">
          <Field label={t("v2.contextLayer.myAuth")} hint={t("v2.contextLayer.wizard.authHint")}>
            <AuthorizationControl status={auth} onChanged={onStatusChanged} testId="v2-agent-context-auth" />
          </Field>
          <Field
            label={t("v2.contextLayer.wizard.namespace")}
            hint={mode === "manual" ? t("v2.contextLayer.wizard.manualHint") : t("v2.contextLayer.wizard.pickHint")}
            error={badId ? t("v2.contextLayer.wizard.badId") : undefined}
          >
            {authorized && namespaces.loading ? (
              <Spin />
            ) : mode === "picker" ? (
              <Select
                value={current?.namespaceId ?? ""}
                placeholder={t("v2.contextLayer.wizard.none")}
                options={namespaceOptions(list ?? [], current)}
                onChange={(id) => setNamespace(pickNamespace(list ?? [], id, current))}
                testId="v2-agent-context-namespace"
              />
            ) : (
              <div className="v2-form cols-2">
                <input
                  className="v2-input mono"
                  value={current?.namespaceId ?? ""}
                  placeholder="dc09046b-28af-4c7b-a03a-4fa63cda243b"
                  onChange={(e) => {
                    const id = e.target.value.trim();
                    setNamespace(id ? { namespaceId: id, namespaceName: current?.namespaceName ?? "" } : null);
                  }}
                  data-testid="v2-agent-context-namespace-id"
                />
                <input
                  className="v2-input"
                  value={current?.namespaceName ?? ""}
                  disabled={!current?.namespaceId}
                  placeholder={t("v2.contextLayer.wizard.namePlaceholder")}
                  onChange={(e) => current && setChoice({ ...current, namespaceName: e.target.value })}
                  data-testid="v2-agent-context-namespace-name"
                />
              </div>
            )}
          </Field>
          {current && (
            <Field
              label={t("v2.contextLayer.wizard.via")}
              hint={t(gatewayEnabled ? "v2.contextLayer.wizard.viaHint" : "v2.contextLayer.wizard.viaGatewayDisabled")}
            >
              <Segmented<ContextLayerVia>
                value={via}
                ariaLabel={t("v2.contextLayer.wizard.via")}
                options={[
                  { value: "direct", label: t("v2.contextLayer.wizard.viaDirect"), testId: "v2-agent-context-via-direct" },
                  {
                    value: "gateway",
                    label: t("v2.contextLayer.wizard.viaGateway"),
                    testId: "v2-agent-context-via-gateway",
                    // a stored gateway-mode choice stays selectable, so it can be switched back
                    disabled: !gatewayEnabled && via !== "gateway",
                    title: gatewayEnabled ? undefined : t("v2.contextLayer.wizard.viaGatewayDisabled"),
                  },
                ]}
                onChange={(next) => setChoice(withContextVia(current, next))}
              />
            </Field>
          )}
          {namespaces.error && <Alert tone="warn">{namespaces.error}</Alert>}
          <div className="v2-row">
            {current && (
              <Button size="sm" onClick={() => setChoice(null)} testId="v2-agent-context-remove">
                {t("v2.contextLayer.wizard.remove")}
              </Button>
            )}
            <Link to="/v2/context-layer" className="v2-link">
              {t("v2.contextLayer.wizard.module")}
            </Link>
          </div>
          <Alert>{t(note)}</Alert>
          {via === "gateway" && form.method !== "harness" && (
            <Alert tone="warn">{t("v2.contextLayer.wizard.gatewayStrandsPending")}</Alert>
          )}
        </div>
      )}
    </Card>
  );
}

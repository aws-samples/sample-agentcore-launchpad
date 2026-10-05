import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { api, errorMessage, type InboundAuthMode } from "../../../lib/api";
import {
  EMPTY_JWT_FORM,
  inboundDraftChanged,
  type JwtFormState,
  jwtConfigFromForm,
  jwtFormFromConfig,
  jwtFormProblem,
} from "../../../lib/inbound-auth";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, Segmented, Spin } from "../../ui";
import { InboundModeTag, JwtConfigFields } from "../agents/InboundAuthFields";

/**
 * The workspace's inbound-auth default: what every HTTP Runtime agent that does
 * not pin its own `inbound_auth` resolves to on its next deploy. Saving never
 * touches deployed agents — each keeps its authorizer until redeployed.
 */
export function InboundDefaultCard({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { can } = useAuth();
  const manage = can("identity.manage");
  const loaded = useLoad(() => api.getInboundAuthDefault(workspaceId), `inbound-default:${workspaceId}`);
  const [mode, setMode] = useState<InboundAuthMode>("iam");
  const [jwt, setJwt] = useState<JwtFormState>({ ...EMPTY_JWT_FORM });
  const [touched, setTouched] = useState(false);
  const [saving, setSaving] = useState(false);
  const data = loaded.data;

  // Back to the saved default: on load, after a save, and on 放弃更改.
  const reset = useCallback(() => {
    if (!data) return;
    setMode(data.default.mode);
    setJwt(jwtFormFromConfig(data.default.jwt));
    setTouched(false);
  }, [data]);
  useEffect(reset, [reset]);
  const changed = !!data && inboundDraftChanged(data.default, mode, jwt);

  const save = async () => {
    setTouched(true);
    if (mode === "jwt" && jwtFormProblem(jwt)) return;
    setSaving(true);
    try {
      await api.putInboundAuthDefault(mode === "jwt" ? { mode, jwt: jwtConfigFromForm(jwt) } : { mode }, workspaceId);
      toast("success", t("inboundAuth.default.saved"));
      loaded.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card
      title={t("inboundAuth.default.title")}
      sub={t("inboundAuth.default.sub")}
      testId="v2-ws-inbound-default"
      end={
        manage && data ? (
          <span className="v2-row">
            <Button disabled={saving || !changed} onClick={reset} testId="v2-ws-inbound-discard">
              {t("inboundAuth.default.discard")}
            </Button>
            <Button kind="primary" disabled={saving} onClick={() => void save()} testId="v2-ws-inbound-save">
              {t("v2.common.save")}
            </Button>
          </span>
        ) : undefined
      }
    >
      {loaded.error && <Alert tone="error">{loaded.error}</Alert>}
      {loaded.loading && !data && <Spin />}
      {data && (
        <>
          <p className="v2-muted">
            {t("inboundAuth.default.current")} <InboundModeTag mode={data.default.mode} />
            {!data.configured && ` ${t("inboundAuth.default.implicit")}`}
          </p>
          {manage ? (
            <>
              <div className="v2-form">
                <Field label={t("inboundAuth.modeLabel")} full>
                  <Segmented<InboundAuthMode>
                    value={mode}
                    ariaLabel={t("inboundAuth.modeLabel")}
                    options={[
                      { value: "iam", label: t("inboundAuth.choice.iam") },
                      { value: "jwt", label: t("inboundAuth.choice.jwt") },
                    ]}
                    onChange={setMode}
                  />
                </Field>
              </div>
              {mode === "jwt" && (
                <>
                  <Alert>{t("inboundAuth.default.jwtNote")}</Alert>
                  <JwtConfigFields
                    form={jwt}
                    onChange={setJwt}
                    showProblem={touched}
                    idPrefix="v2-ws-ia"
                    cognito={data.cognito}
                    cognitoIssuer={data.cognito_issuer ?? null}
                    workspaceId={workspaceId}
                    scope="default"
                  />
                </>
              )}
            </>
          ) : (
            <p className="v2-muted">{t("inboundAuth.default.readOnly")}</p>
          )}
        </>
      )}
    </Card>
  );
}

import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type ContextLayerConnectInput, type ContextLayerStatus, errorMessage } from "../../../lib/api";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Field } from "../../ui";

const PREFIX_RE = /^[a-z][a-z0-9-]{1,30}$/;
const ENV_RE = /^[a-z][a-z0-9-]{0,15}$/;
const REGION_RE = /^[a-z]{2}(-[a-z]+)+-\d{1,2}$/;
const CONNECTION_RE = /^[a-zA-Z0-9\-_]{1,128}$/;

/** The admin's connect (or re-connect) form: Launchpad discovers everything else. */
export function ConnectForm({ current, onConnected }: { current: ContextLayerStatus | null; onConnected: () => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [form, setForm] = useState<ContextLayerConnectInput>({
    prefix: current?.prefix ?? "",
    env: current?.env ?? "dev",
    region: current?.region ?? "",
    connection: current?.connection ?? "",
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const connection = (form.connection ?? "").trim();
  const valid =
    PREFIX_RE.test(form.prefix) && ENV_RE.test(form.env) && REGION_RE.test(form.region) &&
    (!connection || CONNECTION_RE.test(connection));
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.connectContextLayer({ ...form, connection: connection || null });
      toast("success", t("v2.contextLayer.connect.done"));
      onConnected();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const input = (key: keyof ContextLayerConnectInput, placeholder: string) => (
    <input
      className="v2-input mono"
      value={(form[key] as string | null | undefined) ?? ""}
      placeholder={placeholder}
      onChange={(e) => setForm((prev) => ({ ...prev, [key]: e.target.value.trim() }))}
      data-testid={`context-layer-${key}`}
    />
  );
  return (
    <div className="v2-form cols-2" data-testid="context-layer-connect">
      <Field label={t("v2.contextLayer.connect.prefix")} required hint={t("v2.contextLayer.connect.prefixHint")}>
        {input("prefix", "coa-w2")}
      </Field>
      <Field label={t("v2.contextLayer.connect.env")} required>
        {input("env", "dev")}
      </Field>
      <Field label={t("v2.contextLayer.connect.region")} required hint={t("v2.contextLayer.connect.regionHint")}>
        {input("region", "us-west-2")}
      </Field>
      <Field label={t("v2.contextLayer.connect.connection")} hint={t("v2.contextLayer.connect.connectionHint")}>
        {input("connection", `coa-${form.prefix || "<prefix>"}`)}
      </Field>
      {error && (
        <Field label="" full>
          <Alert tone="error">{error}</Alert>
        </Field>
      )}
      <Field label="" full>
        <div className="v2-row">
          <Button kind="primary" disabled={!valid || busy} onClick={() => void submit()} testId="context-layer-connect-submit">
            {busy ? t("v2.contextLayer.connect.busy") : t(current ? "v2.contextLayer.connect.reconnect" : "v2.contextLayer.connect.submit")}
          </Button>
        </div>
      </Field>
    </div>
  );
}

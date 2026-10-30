import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { api, errorMessage, type ModelProviderBody, type ModelProviderKind, type ModelProviderOut, type ModelProviderScope, type ModelProviderUpdateIn } from "../../../lib/api";
import { invalidateCapabilities } from "../../../lib/capabilities";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Drawer, Field, Select } from "../../ui";
import {
  baseUrlProblem,
  defaultCredentialProvider,
  defaultIdPrefix,
  defaultSsmParameter,
  PROVIDER_KINDS,
  PROVIDER_NAME_RE,
  SSM_PARAMETER_RE,
} from "./common";

interface FormState {
  name: string;
  kind: ModelProviderKind;
  label: string;
  base_url: string;
  model_region: string;
  credential_ssm_parameter: string;
  enabled: boolean;
  scope: ModelProviderScope;
  /** N17-obs203 §3: write-only — sent once when non-empty, cleared on submit, never pre-filled */
  api_key: string;
}

const SCOPES: ModelProviderScope[] = ["personal", "workspace"];

function fromProvider(p: ModelProviderOut | null, isAdmin: boolean): FormState {
  return {
    name: p?.name ?? "",
    kind: p?.kind ?? "openai_compatible",
    label: p?.label ?? "",
    base_url: p?.base_url ?? "",
    model_region: p?.model_region ?? "us-west-2", // region-boundary: global-bedrock
    credential_ssm_parameter: p?.credential_ssm_parameter ?? "",
    enabled: p?.enabled ?? true,
    // N17-obs203: a member's provider is personal; an administrator's defaults to workspace
    scope: p?.scope ?? (isAdmin ? "workspace" : "personal"),
    api_key: "",
  };
}

/**
 * Add / edit a provider (plan §8): kind, name, label, base URL (OpenAI-compatible) or
 * model region (Bedrock), the SSM parameter NAME with its pattern hint, enabled. A
 * built-in row locks name and kind. N17-obs203: an administrator picks the scope
 * (personal / workspace — sharing); a member's provider is personal and its SSM parameter
 * name is derived from the owner (shown, not editable). Nothing here accepts a key value
 * (§9.2): the credential itself is registered from the card's *Register credential* action.
 */
export function ProviderDrawer({
  open,
  provider,
  onClose,
  onSaved,
}: {
  open: boolean;
  /** null = add */
  provider: ModelProviderOut | null;
  onClose: () => void;
  onSaved: (p: ModelProviderOut) => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { isAdmin } = useAuth();
  const [form, setForm] = useState<FormState>(() => fromProvider(provider, isAdmin));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [touched, setTouched] = useState(false);
  const editing = provider !== null;
  // name and kind are the row's identity (id prefix, Identity provider name): never editable after create
  const locked = editing;
  // a personal provider's SSM parameter is owner-derived (the server refuses any other name)
  const ssmDerived = form.scope === "personal";
  // N17-obs225: on a shared (workspace-scope) row the endpoint, the SSM parameter and the key
  // are an administrator's — a non-admin creator (a legacy re-pointed row) edits label / enabled
  // only; the server answers 403 `model_provider.forbidden {action}` to anything else
  const credentialLocked = editing && !isAdmin && provider.scope === "workspace";
  // N17-obs228: a host change on a row that holds a registered key either re-enters the key
  // (same save) or clears the stored one — say so before the save, not after
  const hostChanged =
    editing && form.kind === "openai_compatible" && (provider.base_url ?? "").replace(/\/+$/, "") !== form.base_url.trim().replace(/\/+$/, "");
  const hostChangeClearsKey = hostChanged && !!provider?.configured && !form.api_key;
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm((prev) => ({ ...prev, [key]: value }));

  const nameError = !form.name ? t("modelProviders.validation.required") : !PROVIDER_NAME_RE.test(form.name) ? t("modelProviders.validation.namePattern") : null;
  const labelError = !form.label.trim() ? t("modelProviders.validation.required") : null;
  const urlProblem = form.kind === "openai_compatible" ? baseUrlProblem(form.base_url) : null;
  const urlError = urlProblem ? t(`modelProviders.validation.url.${urlProblem}`) : null;
  const ssmValue = ssmDerived ? "" : form.credential_ssm_parameter.trim();
  const ssmError =
    ssmDerived || (form.kind === "bedrock" && provider?.credential_source === "host_env")
      ? null
      : ssmValue && !SSM_PARAMETER_RE.test(ssmValue)
        ? t("modelProviders.validation.ssmPattern")
        : null;
  const valid = !nameError && !labelError && !urlError && !ssmError;

  const submit = async () => {
    setTouched(true);
    if (!valid) return;
    // The `ProviderUpdate` fields. `name` / `kind` are the row's identity: they travel only in
    // the create body — a PUT carrying them answers 422 `model_provider.invalid_update` (D21-obs8).
    const fields: ModelProviderUpdateIn = {
      label: form.label.trim(),
      base_url: form.kind === "openai_compatible" ? form.base_url.trim() : null,
      model_region: form.kind === "bedrock" ? form.model_region.trim() || null : null,
      credential_ssm_parameter: ssmValue || null,
      enabled: form.enabled,
    };
    // N17-obs203: `scope` travels only from an administrator (the server answers 403 to a
    // member's); a member's create body omits it and the server stamps `personal`
    if (isAdmin) fields.scope = form.scope;
    // N17-obs203 §3: the typed key travels once (OpenAI-compatible rows only) and is cleared
    // from the form before the request is even sent
    const typedKey = form.kind === "openai_compatible" ? form.api_key : "";
    if (typedKey) fields.api_key = typedKey;
    set("api_key", "");
    const create: ModelProviderBody = { name: form.name, kind: form.kind, ...fields, label: form.label.trim(), enabled: form.enabled };
    setBusy(true);
    setError(null);
    try {
      const saved = editing ? await api.modelProviders.update(provider.id, fields) : await api.modelProviders.create(create);
      invalidateCapabilities();
      toast("success", t(editing ? "modelProviders.toast.providerSaved" : "modelProviders.toast.providerCreated", { label: saved.label }));
      onSaved(saved);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Drawer
      open={open}
      title={editing ? t("modelProviders.drawer.editProvider", { label: provider.label }) : t("modelProviders.drawer.addProvider")}
      onClose={onClose}
      testId="v2-mp-provider-drawer"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={busy || (touched && !valid)} onClick={() => void submit()} testId="v2-mp-provider-save">
            {t("v2.common.save")}
          </Button>
        </>
      }
    >
      <div className="v2-mp-form">
        {error && <Alert tone="error">{error}</Alert>}
        <div className="cols">
          <Field label={t("modelProviders.field.kind")} required hint={locked ? t("modelProviders.lockedHint") : undefined}>
            <Select
              value={form.kind}
              disabled={locked}
              options={PROVIDER_KINDS.map((kind) => ({ value: kind, label: t(`modelProviders.kind.${kind}`) }))}
              onChange={(value) => set("kind", value as ModelProviderKind)}
              testId="v2-mp-provider-kind"
            />
          </Field>
          <Field
            label={t("modelProviders.field.name")}
            required
            error={touched ? nameError : null}
            hint={t("modelProviders.hint.name", { prefix: defaultIdPrefix(form.name, form.kind) || "—", provider: defaultCredentialProvider(form.name) })}
          >
            <input
              className="v2-input"
              value={form.name}
              disabled={locked}
              placeholder="siliconflow"
              onChange={(e) => set("name", e.target.value.trim().toLowerCase())}
              data-testid="v2-mp-provider-name"
            />
          </Field>
        </div>
        <Field label={t("modelProviders.field.label")} required error={touched ? labelError : null}>
          <input className="v2-input" value={form.label} placeholder="SiliconFlow" onChange={(e) => set("label", e.target.value)} data-testid="v2-mp-provider-label" />
        </Field>
        {form.kind === "openai_compatible" ? (
          <Field
            label={t("modelProviders.field.baseUrl")}
            required
            error={touched ? urlError : null}
            hint={credentialLocked ? t("modelProviders.hint.sharedCredentialLocked") : t("modelProviders.hint.baseUrl")}
          >
            <input
              className="v2-input"
              value={form.base_url}
              disabled={credentialLocked}
              placeholder="https://api.siliconflow.cn/v1"
              onChange={(e) => set("base_url", e.target.value)}
              data-testid="v2-mp-provider-url"
            />
          </Field>
        ) : (
          <Field label={t("modelProviders.field.modelRegion")} hint={t("modelProviders.hint.modelRegion")}>
            <input className="v2-input" value={form.model_region} onChange={(e) => set("model_region", e.target.value)} data-testid="v2-mp-provider-region" />
          </Field>
        )}
        {isAdmin && (
          <Field label={t("modelProviders.field.scope")} hint={t(`modelProviders.scopeHint.${form.scope}`)}>
            <Select
              value={form.scope}
              options={SCOPES.map((scope) => ({ value: scope, label: t(`modelProviders.scope.${scope}`) }))}
              onChange={(value) => set("scope", value as ModelProviderScope)}
              testId="v2-mp-provider-scope"
            />
          </Field>
        )}
        {hostChangeClearsKey && (
          <Alert tone="warn" testId="v2-mp-provider-host-change-warning">{t("modelProviders.hint.hostChangeClearsKey")}</Alert>
        )}
        {form.kind === "openai_compatible" && !credentialLocked && (
          <Field
            label={t(editing ? "modelProviders.field.replaceApiKey" : "modelProviders.field.apiKey")}
            hint={t(editing && provider?.key_last4 ? "modelProviders.hint.apiKeyReplace" : "modelProviders.hint.apiKey", { last4: provider?.key_last4 ?? "" })}
          >
            <input
              className="v2-input"
              type="password"
              autoComplete="off"
              value={form.api_key}
              placeholder={editing ? t("modelProviders.key.keepPlaceholder") : "sk-…"}
              onChange={(e) => set("api_key", e.target.value)}
              data-testid="v2-mp-provider-api-key"
            />
          </Field>
        )}
        {/* N17-obs203 §3 (amendment): the SSM parameter NAME is an operator's advanced option —
            collapsed by default; a personal provider's name is derived and read-only */}
        <details className="v2-mp-advanced" data-testid="v2-mp-provider-advanced">
          <summary>{t("modelProviders.advanced")}</summary>
          {ssmDerived ? (
            <Field label={t("modelProviders.field.ssmParameter")} hint={t("modelProviders.hint.ssmDerived")}>
              <input className="v2-input" value={provider?.credential_ssm_parameter ?? t("modelProviders.hint.ssmDerivedPending")} disabled readOnly data-testid="v2-mp-provider-ssm" />
            </Field>
          ) : (
            <Field
              label={t("modelProviders.field.ssmParameter")}
              error={touched ? ssmError : null}
              hint={t("modelProviders.hint.ssmParameter", { name: defaultSsmParameter(form.name) })}
            >
              <input
                className="v2-input"
                value={form.credential_ssm_parameter}
                disabled={credentialLocked}
                placeholder={defaultSsmParameter(form.name)}
                onChange={(e) => set("credential_ssm_parameter", e.target.value)}
                data-testid="v2-mp-provider-ssm"
              />
            </Field>
          )}
        </details>
        <div className="v2-checks">
          <label className="v2-check">
            <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} data-testid="v2-mp-provider-enabled" />
            {t("modelProviders.field.enabled")}
          </label>
        </div>
        <Alert tone="info">{t("modelProviders.noSecretNote")}</Alert>
      </div>
    </Drawer>
  );
}

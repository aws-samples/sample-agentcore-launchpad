import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  api,
  errorMessage,
  type ModelProviderOut,
  type ModelProviderUpdateIn,
  type ModelRowOut,
} from "../../../lib/api";
import { invalidateCapabilities } from "../../../lib/capabilities";
import { useV2Toast } from "../../hooks";
import { Button, Confirm, Field, Modal } from "../../ui";
import { isInUse, presetsFromError, type Referrer, referrerLink, referrersFromError, rolesFromError } from "./common";

type ConfirmKind = "deleteProvider" | "deleteModel" | "disableProvider";

/**
 * Every registry write of the page: busy + toast handling, the capabilities cache
 * invalidation the pickers depend on (plan §5.5), and the dialogs the writes need —
 * confirmations and the 409 `*.in_use` referrer list (suggesting *disable* instead).
 */
export function useModelProviderActions(onChanged: () => void) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ kind: ConfirmKind; provider: ModelProviderOut; model?: ModelRowOut } | null>(null);
  const [inUse, setInUse] = useState<{ title: string; referrers: Referrer[]; roles: string[]; presets: string[] } | null>(null);
  // N17-obs203 §3: the "replace key" dialog — the typed key lives in this state until the POST
  // returns, then it is cleared; nothing else on the page ever holds it
  const [keyFor, setKeyFor] = useState<ModelProviderOut | null>(null);
  const [typedKey, setTypedKey] = useState("");

  /**
   * Run one write; `true` on success. A 409 in-use answer opens the referrer dialog.
   * `successKey` may be a function of the response when the toast depends on what the
   * server actually did (credential: created / rotated / up to date — D21-obs5).
   */
  const run = async <T,>(
    id: string,
    fn: () => Promise<T>,
    successKey: string | ((result: T) => string),
    params?: Record<string, unknown>,
  ) => {
    setBusyId(id);
    try {
      const result = await fn();
      invalidateCapabilities();
      toast("success", t(typeof successKey === "function" ? successKey(result) : successKey, params));
      onChanged();
      return true;
    } catch (err) {
      if (isInUse(err)) {
        setInUse({ title: t("modelProviders.inUse.title"), referrers: referrersFromError(err), roles: rolesFromError(err), presets: presetsFromError(err) });
      } else {
        toast("error", t("modelProviders.actionFailed", { msg: errorMessage(err) }));
      }
      return false;
    } finally {
      setBusyId(null);
    }
  };

  const toggleProvider = (p: ModelProviderOut) =>
    p.enabled
      ? setConfirm({ kind: "disableProvider", provider: p })
      : void run(p.id, () => api.modelProviders.update(p.id, providerBody(p, { enabled: true })), "modelProviders.toast.providerEnabled", { label: p.label });

  const toggleModel = (p: ModelProviderOut, m: ModelRowOut) =>
    run(
      m.id,
      () => api.modelProviders.updateModel(p.id, m.id, { enabled: !m.enabled }),
      m.enabled ? "modelProviders.toast.modelDisabled" : "modelProviders.toast.modelEnabled",
      { label: m.label },
    );

  const setDefault = (p: ModelProviderOut, m: ModelRowOut) =>
    run(m.id, () => api.modelProviders.updateModel(p.id, m.id, { is_default: true }), "modelProviders.toast.defaultSet", { label: m.label });

  const credential = (p: ModelProviderOut) =>
    run(
      p.id,
      () => api.modelProviders.credential(p.id),
      (r) =>
        r.created
          ? "modelProviders.toast.registered"
          : r.rotated
            ? "modelProviders.toast.rotated"
            : "modelProviders.toast.upToDate",
      { label: p.label },
    );

  const askReplaceKey = (p: ModelProviderOut) => {
    setTypedKey("");
    setKeyFor(p);
  };

  const submitReplaceKey = async () => {
    if (!keyFor || !typedKey.trim()) return;
    const key = typedKey;
    setTypedKey("");
    const ok = await run(
      keyFor.id,
      () => api.modelProviders.credential(keyFor.id, { api_key: key }),
      (r) => (r.last4 ? "modelProviders.toast.keyStored" : "modelProviders.toast.registered"),
      { label: keyFor.label },
    );
    if (ok) setKeyFor(null);
  };

  const askDeleteProvider = (p: ModelProviderOut) => setConfirm({ kind: "deleteProvider", provider: p });
  const askDeleteModel = (p: ModelProviderOut, m: ModelRowOut) => setConfirm({ kind: "deleteModel", provider: p, model: m });

  const setWorkspaceFlags = (workspaceId: string, disabled: string[]) =>
    run(`ws:${workspaceId}`, () => api.modelProviders.setWorkspaceFlags(workspaceId, disabled), "modelProviders.toast.flagsSaved");

  const runConfirm = async () => {
    if (!confirm) return;
    const { kind, provider, model } = confirm;
    let ok = false;
    if (kind === "deleteProvider") {
      ok = await run(provider.id, () => api.modelProviders.remove(provider.id), "modelProviders.toast.providerDeleted", { label: provider.label });
    } else if (kind === "deleteModel" && model) {
      ok = await run(model.id, () => api.modelProviders.removeModel(provider.id, model.id), "modelProviders.toast.modelDeleted", { label: model.label });
    } else if (kind === "disableProvider") {
      ok = await run(provider.id, () => api.modelProviders.update(provider.id, providerBody(provider, { enabled: false })), "modelProviders.toast.providerDisabled", { label: provider.label });
    }
    if (ok || inUse) setConfirm(null);
  };

  const confirmCopy = () => {
    if (!confirm) return null;
    const { kind, provider, model } = confirm;
    switch (kind) {
      case "deleteProvider":
        return { title: t("modelProviders.confirm.deleteProviderTitle"), body: t("modelProviders.confirm.deleteProviderBody", { label: provider.label }), label: t("v2.common.delete"), danger: true };
      case "deleteModel":
        return { title: t("modelProviders.confirm.deleteModelTitle"), body: t("modelProviders.confirm.deleteModelBody", { label: model?.label ?? "" }), label: t("v2.common.delete"), danger: true };
      default:
        return { title: t("modelProviders.confirm.disableProviderTitle"), body: t("modelProviders.confirm.disableProviderBody", { label: provider.label }), label: t("modelProviders.action.disable"), danger: true };
    }
  };

  const renderDialogs = () => {
    const copy = confirmCopy();
    return (
      <>
        <Confirm
          open={copy !== null}
          title={copy?.title ?? ""}
          body={copy?.body ?? ""}
          confirmLabel={copy?.label ?? ""}
          danger={copy?.danger}
          busy={busyId !== null}
          onConfirm={() => void runConfirm()}
          onClose={() => setConfirm(null)}
        />
        <Modal
          open={inUse !== null}
          title={inUse?.title ?? ""}
          onClose={() => setInUse(null)}
          testId="v2-mp-in-use"
          footer={<Button kind="primary" onClick={() => setInUse(null)}>{t("v2.common.close")}</Button>}
        >
          <p className="v2-users-dialog-body">{t("modelProviders.inUse.body")}</p>
          {/* N17-obs207: the platform roles and system presets holding the provider, named */}
          {inUse && inUse.roles.length > 0 && (
            <p className="v2-muted" data-testid="v2-mp-in-use-roles">
              {t("modelProviders.inUse.roles", { roles: inUse.roles.map((r) => t(`modelProviders.roles.role.${r}.label`, r)).join(", ") })}
            </p>
          )}
          {inUse && inUse.presets.length > 0 && (
            <p className="v2-muted" data-testid="v2-mp-in-use-presets">{t("modelProviders.inUse.presets", { presets: inUse.presets.join(", ") })}</p>
          )}
          <ul className="v2-mp-referrers" data-testid="v2-mp-referrers">
            {(inUse?.referrers ?? []).map((ref) => (
              <li key={`${ref.kind}:${ref.id}`}>
                <span className="v2-muted">{t(`modelProviders.referrer.${ref.kind}`, ref.kind)}</span>{" "}
                <Link to={referrerLink(ref)} onClick={() => setInUse(null)}>
                  {ref.name || ref.id}
                </Link>
              </li>
            ))}
            {inUse && inUse.referrers.length === 0 && <li className="v2-muted">{t("modelProviders.inUse.unlisted")}</li>}
          </ul>
        </Modal>
        <Modal
          open={keyFor !== null}
          title={t("modelProviders.key.replaceTitle", { label: keyFor?.label ?? "" })}
          onClose={() => {
            setTypedKey("");
            setKeyFor(null);
          }}
          testId="v2-mp-key-dialog"
          footer={
            <>
              <Button
                onClick={() => {
                  setTypedKey("");
                  setKeyFor(null);
                }}
              >
                {t("v2.common.cancel")}
              </Button>
              <Button kind="primary" disabled={busyId !== null || !typedKey.trim()} onClick={() => void submitReplaceKey()} testId="v2-mp-key-save">
                {t("modelProviders.key.store")}
              </Button>
            </>
          }
        >
          <div className="v2-mp-form">
            <Field label={t("modelProviders.field.apiKey")} required hint={t("modelProviders.hint.apiKey")}>
              <input
                className="v2-input"
                type="password"
                autoComplete="off"
                value={typedKey}
                placeholder="sk-…"
                onChange={(e) => setTypedKey(e.target.value)}
                data-testid="v2-mp-key-input"
              />
            </Field>
            {keyFor?.credential_ssm_parameter && <p className="v2-muted">{t("modelProviders.key.target", { parameter: keyFor.credential_ssm_parameter })}</p>}
          </div>
        </Modal>
      </>
    );
  };

  return {
    busyId,
    run,
    toggleProvider,
    toggleModel,
    setDefault,
    credential,
    askReplaceKey,
    askDeleteProvider,
    askDeleteModel,
    setWorkspaceFlags,
    renderDialogs,
  };
}

/**
 * The `PUT /{id}` body of a provider: the row's current `ProviderUpdate` fields with `patch`
 * applied. `name` and `kind` are immutable and NOT part of `ProviderUpdate` — sending them
 * made every enable / disable answer 422 (D21-obs8); the return type keeps them out.
 */
export function providerBody(p: ModelProviderOut, patch: Partial<Pick<ModelProviderOut, "label" | "base_url" | "model_region" | "credential_ssm_parameter" | "enabled">>): ModelProviderUpdateIn {
  return {
    label: patch.label ?? p.label,
    base_url: patch.base_url !== undefined ? patch.base_url : p.base_url,
    model_region: patch.model_region !== undefined ? patch.model_region : p.model_region,
    credential_ssm_parameter: patch.credential_ssm_parameter !== undefined ? patch.credential_ssm_parameter : p.credential_ssm_parameter,
    enabled: patch.enabled ?? p.enabled,
  };
}

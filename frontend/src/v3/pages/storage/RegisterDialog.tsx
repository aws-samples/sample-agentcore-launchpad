import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage } from "../../../lib/api";
import type { NetworkProfile } from "../../../lib/api/networkProfiles";
import {
  mountedPrefixHint,
  normalizePrefix,
  STORAGE_DATA_CLASSES,
  STORAGE_MODES,
  STORAGE_SCOPES,
  type StorageForm,
  validateStorage,
} from "../../../lib/api/storage";
import { Reasoned } from "../../reason";
import { Btn, Dialog, Notice } from "../../ui";
import { Select } from "../../select";

/**
 * Register / edit a storage entry (storage-mounts §4.4): an S3 Files / EFS access point
 * (only its ARN and an id are needed — the check fills in the rest) or an S3 bucket
 * prefix (bucket, prefix, layout, and — for another account or Region — owner and
 * Region). The id and what it points at are fixed once registered (agents name the id;
 * a stamped reference keeps the ARN or the bucket fields).
 */
export function RegisterDialog({
  initial,
  creating,
  profiles,
  onClose,
  onSubmit,
}: {
  initial: StorageForm;
  creating: boolean;
  profiles: NetworkProfile[];
  onClose: () => void;
  onSubmit: (form: StorageForm) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<StorageForm>(initial);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const errors = touched ? validateStorage(form, creating) : {};
  const set = <K extends keyof StorageForm>(key: K, value: StorageForm[K]) => {
    setError(null);
    setForm((prev) => ({ ...prev, [key]: value }));
  };
  const submit = async () => {
    setTouched(true);
    if (Object.keys(validateStorage(form, creating)).length > 0) return;
    setBusy(true);
    setError(null);
    try {
      await onSubmit(form);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  const field = (key: keyof StorageForm, label: string, input: ReactNode, hint?: string) => (
    <label className="v3-field">
      <span>{label}</span>
      {input}
      {errors[key] ? <small className="v3-err">{t(errors[key] as string)}</small> : hint ? <small className="v3-hint">{hint}</small> : null}
    </label>
  );

  return (
    <Dialog
      wide
      title={t(creating ? "v3.storage.form.createTitle" : "v3.storage.form.editTitle")}
      onClose={() => !busy && onClose()}
      foot={
        <>
          <Btn kind="ghost" disabled={busy} onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" disabled={busy} onClick={() => void submit()}>{t(creating ? "v3.storage.form.create" : "v3.storage.form.save")}</Btn>
        </>
      }
    >
      <form style={{ display: "grid", gap: 12 }} data-testid="v3-storage-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        {field("id", t("v3.storage.form.id"),
          <Reasoned disabledReason={t("story.reason.fixedOnceCreated")}>
            <input className="v3-input mono" placeholder="datasets" value={form.id} disabled={!creating}
              aria-invalid={errors.id ? true : undefined} onChange={(e) => set("id", e.target.value)} />
          </Reasoned>,
          t("v3.storage.form.idHint"))}
        {field("name", t("v3.storage.form.name"),
          <input className="v3-input" value={form.name} aria-invalid={errors.name ? true : undefined} onChange={(e) => set("name", e.target.value)} />)}
        {creating && (
          <label className="v3-field">
            <span>{t("v3.storage.form.kind")}</span>
            <Select className="v3-select" value={form.kind === "s3bucket" ? "s3bucket" : "accessPoint"}
              onChange={(e) => set("kind", e.target.value === "s3bucket" ? "s3bucket" : "s3files")} data-testid="v3-storage-kind">
              <option value="accessPoint">{t("v3.storage.form.kindAccessPoint")}</option>
              <option value="s3bucket">{t("v3.storage.form.kindBucket")}</option>
            </Select>
          </label>
        )}
        {form.kind === "s3bucket" ? (
          <>
            {field("bucket", t("v3.storage.form.bucket"),
              <Reasoned disabledReason={t("story.reason.fixedOnceCreated")}>
                <input className="v3-input mono" disabled={!creating} value={form.bucket} placeholder="acme-agent-data"
                  aria-invalid={errors.bucket ? true : undefined} onChange={(e) => set("bucket", e.target.value)} />
              </Reasoned>,
              t("v3.storage.form.bucketHint"))}
            {field("prefix", t("v3.storage.form.prefix"),
              <Reasoned disabledReason={t("story.reason.fixedOnceCreated")}>
                <input className="v3-input mono" disabled={!creating} value={form.prefix} placeholder="team/data"
                  aria-invalid={errors.prefix ? true : undefined} onChange={(e) => set("prefix", e.target.value)} />
              </Reasoned>,
              t("v3.storage.form.prefixHint"))}
            <fieldset className="v3-field" style={{ border: 0, padding: 0, margin: 0 }}>
              <span>{t("v3.storage.form.scope")}</span>
              {/* fixed once registered (a stamped reference keeps it): shown, not editable */}
              {(creating ? STORAGE_SCOPES : [form.scope]).map((scope) => (
                <label key={scope} style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
                  {creating && (
                    <input type="radio" name="v3-storage-scope" value={scope} checked={form.scope === scope}
                      onChange={() => set("scope", scope)} data-testid={`v3-storage-scope-${scope}`} />
                  )}
                  <span>{t(scope === "per_session" ? "v3.storage.form.scopePerSession" : "v3.storage.form.scopeShared")}</span>
                </label>
              ))}
              <small className="v3-hint" data-testid="v3-storage-scope-hint">
                {t(form.scope === "per_session" ? "v3.storage.form.scopePerSessionHint" : "v3.storage.form.scopeSharedHint", {
                  path: mountedPrefixHint({ bucket: form.bucket.trim() || "<bucket>", prefix: normalizePrefix(form.prefix), scope: form.scope }),
                })}
              </small>
            </fieldset>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              {field("bucket_owner", t("v3.storage.form.owner"),
                <Reasoned disabledReason={t("story.reason.fixedOnceCreated")}>
                  <input className="v3-input mono" disabled={!creating} value={form.bucket_owner} placeholder="111122223333"
                    aria-invalid={errors.bucket_owner ? true : undefined} onChange={(e) => set("bucket_owner", e.target.value)} />
                </Reasoned>,
                t("v3.storage.form.ownerHint"))}
              {field("bucket_region", t("v3.storage.form.region"),
                <Reasoned disabledReason={t("story.reason.fixedOnceCreated")}>
                  <input className="v3-input mono" disabled={!creating} value={form.bucket_region} placeholder="us-east-1"
                    aria-invalid={errors.bucket_region ? true : undefined} onChange={(e) => set("bucket_region", e.target.value)} />
                </Reasoned>,
                t("v3.storage.form.regionHint"))}
            </div>
          </>
        ) : field("access_point_arn", t("v3.storage.form.arn"),
          <Reasoned disabledReason={t("story.reason.fixedOnceCreated")}>
            <input className="v3-input mono" disabled={!creating} value={form.access_point_arn}
              placeholder="arn:aws:s3files:us-west-2:111122223333:file-system/fs-…/access-point/fsap-…"
              aria-invalid={errors.access_point_arn ? true : undefined} onChange={(e) => set("access_point_arn", e.target.value)} />
          </Reasoned>,
          t("v3.storage.form.arnHint"))}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <label className="v3-field">
            <span>{t("v3.storage.form.maxMode")}</span>
            <Select className="v3-select" value={form.max_mode} onChange={(e) => set("max_mode", e.target.value as StorageForm["max_mode"])}>
              {STORAGE_MODES.map((m) => <option key={m} value={m}>{t(`v3.storage.mode.${m}`)}</option>)}
            </Select>
          </label>
          <label className="v3-field">
            <span>{t("v3.storage.form.dataClass")}</span>
            <Select className="v3-select" value={form.data_class} onChange={(e) => set("data_class", e.target.value as StorageForm["data_class"])}>
              {STORAGE_DATA_CLASSES.map((c) => <option key={c} value={c}>{t(`v3.storage.dataClass.${c}`)}</option>)}
            </Select>
          </label>
        </div>
        <label className="v3-field">
          <span>{t("v3.storage.form.profile")}</span>
          <Select className="v3-select" value={form.network_profile_id} onChange={(e) => set("network_profile_id", e.target.value)}>
            <option value="">{t("v3.storage.form.noProfile")}</option>
            {profiles.filter((p) => !p.archived_at).map((p) => <option key={p.id} value={p.id}>{p.name} ({p.id})</option>)}
          </Select>
          <small className="v3-hint">{t(form.kind === "s3bucket" ? "v3.storage.form.profileHintBucket" : "v3.storage.form.profileHint")}</small>
        </label>
        {form.max_mode === "rw" && <Notice s="info">{t("v3.storage.form.rwHint")}</Notice>}
        {error && <Notice s="act">{error}</Notice>}
        <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
      </form>
    </Dialog>
  );
}

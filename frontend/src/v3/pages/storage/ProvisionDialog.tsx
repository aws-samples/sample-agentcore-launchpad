import { type ReactNode, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage } from "../../../lib/api";
import type { NetworkProfile } from "../../../lib/api/networkProfiles";
import {
  emptyProvisionForm,
  type ProvisionForm,
  STORAGE_DATA_CLASSES,
  STORAGE_MODES,
  storageApi,
  type StorageProvisionPlan,
  validateProvision,
} from "../../../lib/api/storage";
import { Select } from "../../select";
import { Btn, Dialog, Notice } from "../../ui";
import { ProvisionPlan } from "./ProvisionPlan";

/**
 * Create S3 Files storage (storage-mounts §7): the console makes a new bucket, the file
 * system, its mount targets in the network profile's Availability Zones, a mount-target
 * security group and the access point, then registers the result. Only the id, a
 * network profile and the access point's POSIX identity / root directory are asked for.
 */
export function ProvisionDialog({
  profiles,
  onClose,
  onSubmit,
}: {
  profiles: NetworkProfile[];
  onClose: () => void;
  onSubmit: (form: ProvisionForm) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<ProvisionForm>(emptyProvisionForm);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // what the create would add to the profile's VPC, and the administrator's
  // acknowledgement of it: nothing is submitted before both
  const [plan, setPlan] = useState<StorageProvisionPlan | null>(null);
  const [planError, setPlanError] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const errors = touched ? validateProvision(form) : {};
  const live = profiles.filter((p) => !p.archived_at);
  const profileId = form.network_profile_id;
  useEffect(() => {
    setPlan(null);
    setPlanError(null);
    setAcknowledged(false);
    if (!profileId) return;
    let stale = false;
    storageApi.provisionPlan(profileId).then(
      (p) => { if (!stale) setPlan(p); },
      (err) => { if (!stale) setPlanError(errorMessage(err)); },
    );
    return () => { stale = true; };
  }, [profileId]);
  const set = <K extends keyof ProvisionForm>(key: K, value: ProvisionForm[K]) => {
    setError(null);
    setForm((prev) => ({ ...prev, [key]: value }));
  };
  const submit = async () => {
    setTouched(true);
    if (Object.keys(validateProvision(form)).length > 0 || !acknowledged) return;
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
  const field = (key: keyof ProvisionForm, label: string, input: ReactNode, hint?: string) => (
    <label className="v3-field">
      <span>{label}</span>
      {input}
      {errors[key] ? <small className="v3-err">{t(errors[key] as string)}</small> : hint ? <small className="v3-hint">{hint}</small> : null}
    </label>
  );

  return (
    <Dialog
      wide
      title={t("v3.storage.provision.title")}
      onClose={() => !busy && onClose()}
      foot={
        <>
          <Btn kind="ghost" disabled={busy} onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" busy={busy} disabled={busy || live.length === 0 || !acknowledged}
            disabledReason={live.length === 0 ? t("v3.storage.provision.noProfiles")
              : !acknowledged ? t("v3.storage.provision.plan.ackFirst") : undefined}
            onClick={() => void submit()}>
            {t("v3.storage.provision.submit")}
          </Btn>
        </>
      }
    >
      <form style={{ display: "grid", gap: 12 }} data-testid="v3-storage-provision-form"
        onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <Notice s="info">{t("v3.storage.provision.intro")}</Notice>
        <small className="v3-hint">{t("v3.storage.provision.adminOnly")}</small>
        {live.length === 0 && <Notice s="act">{t("v3.storage.provision.noProfiles")}</Notice>}
        {field("id", t("v3.storage.form.id"),
          <input className="v3-input mono" placeholder="datasets" value={form.id}
            aria-invalid={errors.id ? true : undefined} onChange={(e) => set("id", e.target.value)} />,
          t("v3.storage.form.idHint"))}
        {field("name", t("v3.storage.form.name"),
          <input className="v3-input" value={form.name} aria-invalid={errors.name ? true : undefined}
            onChange={(e) => set("name", e.target.value)} />)}
        {field("network_profile_id", t("v3.storage.form.profile"),
          <Select className="v3-select" value={form.network_profile_id}
            onChange={(e) => set("network_profile_id", e.target.value)}>
            <option value="">{t("v3.storage.provision.pickProfile")}</option>
            {live.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.id})</option>)}
          </Select>,
          t("v3.storage.provision.profileHint"))}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 2fr", gap: 12 }}>
          {field("posix_uid", t("v3.storage.provision.uid"),
            <input className="v3-input mono" inputMode="numeric" value={form.posix_uid}
              aria-invalid={errors.posix_uid ? true : undefined} onChange={(e) => set("posix_uid", e.target.value)} />)}
          {field("posix_gid", t("v3.storage.provision.gid"),
            <input className="v3-input mono" inputMode="numeric" value={form.posix_gid}
              aria-invalid={errors.posix_gid ? true : undefined} onChange={(e) => set("posix_gid", e.target.value)} />)}
          {field("root_directory", t("v3.storage.provision.root"),
            <input className="v3-input mono" value={form.root_directory}
              aria-invalid={errors.root_directory ? true : undefined} onChange={(e) => set("root_directory", e.target.value)} />,
            t("v3.storage.provision.rootHint"))}
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
          <label className="v3-field">
            <span>{t("v3.storage.form.maxMode")}</span>
            <Select className="v3-select" value={form.max_mode} onChange={(e) => set("max_mode", e.target.value as ProvisionForm["max_mode"])}>
              {STORAGE_MODES.map((m) => <option key={m} value={m}>{t(`v3.storage.mode.${m}`)}</option>)}
            </Select>
          </label>
          <label className="v3-field">
            <span>{t("v3.storage.form.dataClass")}</span>
            <Select className="v3-select" value={form.data_class} onChange={(e) => set("data_class", e.target.value as ProvisionForm["data_class"])}>
              {STORAGE_DATA_CLASSES.map((c) => <option key={c} value={c}>{t(`v3.storage.dataClass.${c}`)}</option>)}
            </Select>
          </label>
        </div>
        {profileId && !plan && !planError && <small className="v3-hint">{t("v3.storage.provision.plan.loading")}</small>}
        {planError && <Notice s="act">{t("v3.storage.provision.plan.failed", { error: planError })}</Notice>}
        {plan && <ProvisionPlan plan={plan} storageId={form.id} />}
        <small className="v3-hint">{t("v3.storage.provision.cost")}</small>
        {profileId && (plan || planError) && (
          <label style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
            <input type="checkbox" data-testid="v3-storage-provision-ack" checked={acknowledged}
              onChange={(e) => setAcknowledged(e.target.checked)} />
            <span>{t("v3.storage.provision.plan.ack")}</span>
          </label>
        )}
        {error && <Notice s="act">{error}</Notice>}
        <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
      </form>
    </Dialog>
  );
}

import { RotateCcw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { StorageProvisionJob } from "../../../lib/api/storage";
import { Btn, Chip, Dialog, Lamp, Notice, Panel } from "../../ui";

const STAGE_SIGNAL: Record<string, "ok" | "wait" | "act" | "off"> = {
  succeeded: "ok", running: "wait", failed: "act", pending: "off",
};
const JOB_SIGNAL: Record<string, "ok" | "wait" | "act"> = {
  succeeded: "ok", queued: "wait", running: "wait", failed: "act",
};

/**
 * The console's S3 Files jobs (storage-mounts §7) still worth showing — running ones and
 * a failed latest one — stage by stage. A failed create offers its retry, which resumes
 * the same job from the stage that failed.
 */
export function ProvisionJobs({
  jobs,
  onRetry,
}: {
  jobs: StorageProvisionJob[];
  onRetry: (job: StorageProvisionJob) => void;
}) {
  const { t } = useTranslation();
  if (jobs.length === 0) return null;
  return (
    <Panel title={t("v3.storage.provision.jobsTitle")}>
      <div style={{ display: "grid", gap: 14 }} data-testid="v3-storage-jobs">
        {jobs.map((job) => (
          <div key={job.job_id} style={{ display: "grid", gap: 6 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <b className="mono">{job.storage_id}</b>
              <span>{t(`v3.storage.provision.op.${job.op}`)}</span>
              <Chip s={JOB_SIGNAL[job.status] ?? "off"}>{t(`v3.storage.provision.job.${job.status}`)}</Chip>
              {job.status === "failed" && job.op === "create" && (
                <Btn size="sm" kind="ghost" onClick={() => onRetry(job)}><RotateCcw size={13} /> {t("v3.storage.provision.retry")}</Btn>
              )}
            </div>
            <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 4 }}>
              {job.stages.map((s) => (
                <li key={s.name} style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
                  <Lamp s={STAGE_SIGNAL[s.status] ?? "off"} title={s.status} />
                  <span style={{ minWidth: 150 }}>{t(`v3.storage.provision.stage.${s.name}`, { defaultValue: s.name })}</span>
                  <small className="mono v3-hint" style={{ overflowWrap: "anywhere" }}>{s.detail}</small>
                </li>
              ))}
            </ol>
            {job.error && <Notice s="act">{job.error}</Notice>}
          </div>
        ))}
      </div>
    </Panel>
  );
}

/** Delete what the console made for an entry: the id must be typed again (§7). */
export function TeardownDialog({
  storageId,
  name,
  onClose,
  onConfirm,
}: {
  storageId: string;
  name: string;
  onClose: () => void;
  onConfirm: (confirm: string) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await onConfirm(typed);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      title={t("v3.storage.teardown.title", { name })}
      onClose={() => !busy && onClose()}
      foot={
        <>
          <Btn kind="ghost" disabled={busy} onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="danger" busy={busy} disabled={busy || typed !== storageId}
            disabledReason={typed !== storageId ? t("v3.storage.teardown.typeId", { id: storageId }) : undefined}
            onClick={() => void confirm()}>
            {t("v3.storage.teardown.confirm")}
          </Btn>
        </>
      }
    >
      <div style={{ display: "grid", gap: 12 }} data-testid="v3-storage-teardown">
        <p style={{ margin: 0 }}>{t("v3.storage.teardown.body", { name })}</p>
        <label className="v3-field">
          <span>{t("v3.storage.teardown.typeId", { id: storageId })}</span>
          <input className="v3-input mono" value={typed} onChange={(e) => setTyped(e.target.value)} />
        </label>
        {error && <Notice s="act">{error}</Notice>}
      </div>
    </Dialog>
  );
}

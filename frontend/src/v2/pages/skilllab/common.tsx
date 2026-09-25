import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { SkillLabJobInfo, SkillLabStatus } from "../../../lib/api";
import { Alert, Button, Tag, type TagTone } from "../../ui";


const STATUS_TONE: Record<string, TagTone> = {
  queued: "gray",
  running: "blue",
  succeeded: "green",
  failed: "red",
  cancelled: "gray",
  interrupted: "orange",
};

/** Job status (with the queue position while it waits behind another job). */
export function JobStatusTag({ job }: { job: SkillLabJobInfo }) {
  const { t } = useTranslation();
  const queued = job.status === "queued" && job.queue_position > 0;
  return (
    <Tag tone={STATUS_TONE[job.status] ?? "gray"} dot>
      {t(`skillLab.eval.status.${job.status}`, { defaultValue: job.status })}
      {queued ? ` #${job.queue_position}` : ""}
    </Tag>
  );
}




/**
 * Worker provisioning gaps. The second one (no local interpreter) blocks
 * task-set authoring too — the validator runs on that interpreter — so it
 * cannot stay silent.
 */
export function ProvisionAlert({ status }: { status: SkillLabStatus | null }) {
  const { t } = useTranslation();
  const [dismissed, setDismissed] = useState(false);
  if (status === null || dismissed || (status.provisioned && status.venv_ready)) return null;
  return (
    <div data-testid="v2-skilllab-unprovisioned">
      <Alert
        tone="warn"
        action={
          <Button size="sm" onClick={() => setDismissed(true)}>
            {t("v2.common.close")}
          </Button>
        }
      >
        {!status.provisioned && <div>{t("skillLab.unprovisioned.body")}</div>}
        {status.missing.length > 0 && <div className="mono v2-muted">{status.missing.join(" · ")}</div>}
        {!status.venv_ready && <div>{t("skillLab.unprovisioned.venv")}</div>}
      </Alert>
    </div>
  );
}

/** A job that no longer exists in this workspace (deleted, or another workspace's). */
export function GoneCard({ title, body, onBack }: { title: string; body: string; onBack: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="v2-card">
      <div className="v2-card-body">
        <h2 className="v2-sec-title">{title}</h2>
        <Alert tone="warn">{body}</Alert>
        <div style={{ marginTop: 12 }}>
          <Button onClick={onBack}>{t("v2.common.back")}</Button>
        </div>
      </div>
    </div>
  );
}

import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  api,
  errorMessage,
  type SkillLabAssetDescriptor,
  type SkillLabStatus,
  type SkillLabTargetBackend,
} from "../../../lib/api";
import { modelDefault } from "../../../lib/skillLab";
import { TASK_ASSET_ACCEPT } from "../../../lib/skillLabTasksets";
import { Alert, Button, Card, Field, FlowHeader, LinkButton } from "../../ui";
import { ModelFields } from "./ModelFields";
import { SkillMultiPicker } from "./SkillPicker";
import { useTasksets } from "./state";

/** Mirrors runner.TASKGEN_ATTACHMENT_DIR: where the agent sees an attached document. */
const RUNTIME_ATTACHMENT_DIR = "data";

const clamp = (value: number, min: number, max: number) => Math.max(min, Math.min(max, value));

/**
 * AI task-set generation (studio parity): registry skill(s), an exec backend
 * and a count; the agent authors tasks on the AgentCore worker. Nothing is
 * saved until the reviewed output is explicitly imported (or appended to the
 * expansion target chosen here).
 */
export function TaskgenWizard({ status }: { status: SkillLabStatus | null }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const tasksets = useTasksets("skill-lab-tasksets:taskgen");
  const [recordIds, setRecordIds] = useState<string[]>([]);
  const [backend, setBackend] = useState<SkillLabTargetBackend>("claude_code_exec");
  const [model, setModel] = useState("");
  const [count, setCount] = useState(5);
  const [guidance, setGuidance] = useState("");
  const [timeout, setTimeoutSeconds] = useState(900);
  const [expandId, setExpandId] = useState("");
  const [targetSplit, setTargetSplit] = useState("tasks");
  // Staged through the shared task-asset endpoint: already verified descriptors.
  const [attachments, setAttachments] = useState<SkillLabAssetDescriptor[]>([]);
  const [attachBusy, setAttachBusy] = useState(false);
  const attachInput = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setModel((prev) => prev || modelDefault(status, backend));
  }, [status, backend]);

  // samples are read-only — expanding one would 409 at submit
  const expandable = (tasksets.data ?? []).filter((row) => !row.sample);
  const expandTarget = expandable.find((row) => row.id === expandId) ?? null;
  const splitOptions = expandTarget === null ? [] : expandTarget.mode === "single" ? ["tasks"] : ["train", "val", "test"];
  useEffect(() => {
    setTargetSplit(expandTarget?.mode === "split" ? "train" : "tasks");
  }, [expandTarget?.id, expandTarget?.mode]);

  const ready = Boolean(status?.provisioned && status.venv_ready);
  const back = () => setParams({ tab: "taskgen" });

  const uploadAttachments = async (files: File[]) => {
    if (!files.length) return;
    setError(null);
    setAttachBusy(true);
    try {
      const response = await api.skillLabTaskAssetsUpload(files);
      setAttachments((prev) => [...prev, ...response.assets]);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setAttachBusy(false);
    }
  };

  const submit = async () => {
    setError(null);
    if (recordIds.length === 0) {
      setError(t("skillLab.taskgen.err.noSkill"));
      return;
    }
    setBusy(true);
    try {
      const job = await api.skillLabJobCreate({
        type: "taskgen",
        skill_source:
          recordIds.length === 1
            ? { kind: "registry", record_id: recordIds[0] }
            : { kind: "registry", record_ids: recordIds },
        ...(expandId ? { taskset_id: expandId, target_split: targetSplit } : {}),
        ...(attachments.length
          ? { attachments: attachments.map((asset) => ({ staged_asset: String(asset.staged_asset) })) }
          : {}),
        params: { target_backend: backend, model: model.trim(), count, guidance: guidance.trim(), timeout },
      });
      setParams({ tab: "taskgen", view: "detail", id: job.id });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <FlowHeader
        title={t("skillLab.taskgen.wizard.title")}
        onBack={back}
        end={
          <>
            <Button onClick={back}>{t("v2.common.cancel")}</Button>
            <Button kind="primary" disabled={busy || !ready} onClick={() => void submit()} testId="v2-taskgen-submit">
              {busy ? t("skillLab.taskgen.wizard.submitting") : t("skillLab.taskgen.wizard.submit")}
            </Button>
          </>
        }
      />
      {!ready && status !== null && <Alert tone="warn">{t("skillLab.eval.wizard.blocked")}</Alert>}
      {error && <Alert tone="error">{error}</Alert>}
      <Card title={t("skillLab.taskgen.field.skills")} sub={t("skillLab.taskgen.hint.skills")}>
        <SkillMultiPicker selected={recordIds} onChange={setRecordIds} testId="v2-taskgen-skills" />
      </Card>
      <Card title={t("v2.skillLab.generation")} sub={t("skillLab.taskgen.wizard.sub")}>
        <ModelFields
          status={status}
          testId="v2-taskgen"
          backend={backend}
          setBackend={setBackend}
          targetModel={model}
          setTargetModel={setModel}
        />
        <div className="v2-form cols-2" style={{ marginTop: 16 }}>
          <Field label={t("skillLab.taskgen.field.count")} hint={t("skillLab.taskgen.hint.count", { min: 1, max: 30 })}>
            <input
              className="v2-input"
              type="number"
              min={1}
              max={30}
              value={count}
              data-testid="v2-taskgen-count"
              onChange={(e) => setCount(clamp(Number(e.target.value) || 1, 1, 30))}
            />
          </Field>
          <Field label={t("skillLab.taskgen.field.timeout")} hint={t("skillLab.taskgen.hint.timeout", { min: 60, max: 3600 })}>
            <input
              className="v2-input"
              type="number"
              min={60}
              max={3600}
              value={timeout}
              onChange={(e) => setTimeoutSeconds(Number(e.target.value))}
            />
          </Field>
          <Field label={t("skillLab.taskgen.field.guidance")} full>
            <textarea
              className="v2-textarea"
              rows={3}
              value={guidance}
              placeholder={t("skillLab.taskgen.hint.guidance")}
              data-testid="v2-taskgen-guidance"
              onChange={(e) => setGuidance(e.target.value)}
            />
          </Field>
        </div>
      </Card>
      <Card title={t("skillLab.taskgen.field.attachments")} sub={t("skillLab.taskgen.attach.hint")}>
        <div className="v2-row">
          <Button disabled={attachBusy} onClick={() => attachInput.current?.click()} testId="v2-taskgen-attach">
            {attachBusy ? t("skillLab.taskgen.attach.uploading") : t("skillLab.taskgen.attach.pick")}
          </Button>
          <span className="v2-muted">{t("skillLab.tasksets.assets.hint")}</span>
        </div>
        <input
          ref={attachInput}
          type="file"
          multiple
          accept={TASK_ASSET_ACCEPT}
          style={{ display: "none" }}
          disabled={attachBusy}
          data-testid="v2-taskgen-attach-input"
          onChange={(event) => {
            const picked = Array.from(event.target.files ?? []);
            event.target.value = "";
            void uploadAttachments(picked);
          }}
        />
        {attachments.length > 0 && (
          <div className="v2-stack" style={{ marginTop: 12 }}>
            {attachments.map((asset) => (
              <div key={String(asset.staged_asset)} className="v2-skilllab-asset" data-testid={`v2-taskgen-attachment-${asset.name}`}>
                <span className="mono">{`${RUNTIME_ATTACHMENT_DIR}/${asset.name}`}</span>
                <span className="v2-muted">
                  {asset.media_type} · {asset.size.toLocaleString()} B
                </span>
                <LinkButton
                  danger
                  onClick={() => setAttachments((prev) => prev.filter((row) => row.staged_asset !== asset.staged_asset))}
                >
                  {t("skillLab.taskgen.attach.remove")}
                </LinkButton>
              </div>
            ))}
          </div>
        )}
      </Card>
      <Card title={t("skillLab.taskgen.field.expand")} sub={t("skillLab.taskgen.hint.expand")}>
        <div className="v2-form cols-2">
          <Field label={t("skillLab.taskgen.field.expand")}>
            <select className="v2-select" value={expandId} onChange={(e) => setExpandId(e.target.value)} data-testid="v2-taskgen-expand">
              <option value="">{t("skillLab.taskgen.expand.none")}</option>
              {expandable.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.name} ({t(`skillLab.tasksets.mode.${row.mode}`)})
                </option>
              ))}
            </select>
          </Field>
          {expandTarget !== null && (
            <Field label={t("skillLab.taskgen.field.targetSplit")}>
              <select className="v2-select" value={targetSplit} onChange={(e) => setTargetSplit(e.target.value)} data-testid="v2-taskgen-split">
                {splitOptions.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
            </Field>
          )}
        </div>
      </Card>
    </>
  );
}

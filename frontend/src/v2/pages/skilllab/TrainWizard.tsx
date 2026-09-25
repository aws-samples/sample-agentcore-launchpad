import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  api,
  errorMessage,
  type SkillLabGateMetric,
  type SkillLabJudgeMode,
  type SkillLabStatus,
  type SkillLabTargetBackend,
} from "../../../lib/api";
import { canSubmitJobs, trainRunEstimate, trainSplitCounts } from "../../../lib/skillLab";
import { Alert, Button, Card, Field, FlowHeader } from "../../ui";
import { NumberField } from "./EvalWizard";
import { ModelFields } from "./ModelFields";
import { SkillSourcePicker } from "./SkillPicker";
import { initialSource, skillSourceOf, type SourceState, useTasksets } from "./state";

const GATE_METRICS: SkillLabGateMetric[] = ["hard", "soft", "mixed"];

/** `.md` files inside the skill, minus SKILL.md (which is always trainable). */
const mdOnly = (files: string[]) => files.filter((f) => f.toLowerCase().endsWith(".md") && f !== "SKILL.md");

/**
 * New optimization run (`?tab=train&view=new[&record=][&taskset=]`). Training
 * owns the whole task set (a single-mode set is auto-split 4:3:3 by the loader),
 * so there is no split choice; the held-out gate keeps an edit only when the
 * val score improves.
 */
export function TrainWizard({ status }: { status: SkillLabStatus | null }) {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tasksets = useTasksets("skill-lab-tasksets:train-wizard");
  const [source, setSource] = useState<SourceState>(() => initialSource(params.get("record")));
  const [tasksetId, setTasksetId] = useState(params.get("taskset") ?? "");
  // trainable_files candidates (upstream studio parity). Unchecked = SKILL.md-only.
  const [candidateFiles, setCandidateFiles] = useState<string[]>([]);
  const [trainableFiles, setTrainableFiles] = useState<string[]>([]);
  const [epochs, setEpochs] = useState(1);
  const [learningRate, setLearningRate] = useState(4);
  const [gateMetric, setGateMetric] = useState<SkillLabGateMetric>("soft");
  const [backend, setBackend] = useState<SkillLabTargetBackend>("claude_code_exec");
  const [judgeMode, setJudgeMode] = useState<SkillLabJudgeMode>("auto");
  const [targetModel, setTargetModel] = useState("");
  const [judgeModel, setJudgeModel] = useState("");
  const [workers, setWorkers] = useState(2);
  const [timeout, setTimeoutSeconds] = useState(900);
  const [limit, setLimit] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const provisioned = canSubmitJobs(status);

  const rows = useMemo(() => tasksets.data ?? [], [tasksets.data]);
  // Prefer a split set: it carries the held-out val split the gate wants.
  useEffect(() => {
    if (!rows.length) return;
    const preferred = rows.find((row) => row.mode === "split") ?? rows[0];
    setTasksetId((prev) => prev || preferred.id);
  }, [rows]);
  useEffect(() => {
    if (status === null) return;
    setTargetModel((prev) => prev || status.default_target_model);
    setJudgeModel((prev) => prev || status.default_judge_model);
  }, [status]);

  // Candidate files follow the selected source. Registry: the record's
  // skillDefinition already carries the bundle's file list; upload: the staged
  // inspection result carries it.
  const staged = source.staging?.skills.find((skill) => skill.index === source.stagedIndex) ?? null;
  useEffect(() => {
    setTrainableFiles([]);
    setCandidateFiles([]);
    if (source.tab === "upload") {
      if (staged) setCandidateFiles(mdOnly(staged.files));
      return;
    }
    if (!source.recordId) return;
    let stale = false;
    api
      .v2SkillLabRecord(source.recordId)
      .then((record) => {
        const skills = (record.descriptors as { agentSkills?: { skillDefinition?: { inlineContent?: string } } } | undefined)
          ?.agentSkills;
        const inline = skills?.skillDefinition?.inlineContent;
        if (!inline || stale) return;
        const definition = JSON.parse(inline) as { files?: string[] };
        setCandidateFiles(mdOnly(definition.files ?? []));
      })
      .catch(() => {
        /* no candidates — the section simply stays hidden */
      });
    return () => {
      stale = true;
    };
  }, [source.tab, source.recordId, staged]);

  const selected = rows.find((row) => row.id === tasksetId) ?? null;
  const counts = trainSplitCounts(selected);
  const estimate = trainRunEstimate(selected, epochs);
  const back = () => setParams({ tab: "train" });

  const submit = async () => {
    setError(null);
    const skillSource = skillSourceOf(source);
    if (skillSource === null) {
      setError(t("skillLab.train.wizard.err.noSkill"));
      return;
    }
    if (!tasksetId) {
      setError(t("skillLab.train.wizard.err.noTaskset"));
      return;
    }
    setBusy(true);
    try {
      const job = await api.skillLabJobCreate({
        type: "train",
        skill_source: skillSource,
        taskset_id: tasksetId,
        params: {
          target_backend: backend,
          target_model: targetModel.trim(),
          judge_model: judgeModel.trim(),
          judge_mode: judgeMode,
          epochs,
          learning_rate: learningRate,
          gate_metric: gateMetric,
          workers,
          timeout,
          limit,
          ...(trainableFiles.length > 0 ? { trainable_files: trainableFiles } : {}),
        },
      });
      setParams({ tab: "train", view: "detail", id: job.id });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <FlowHeader
        title={t("skillLab.train.wizard.title")}
        onBack={back}
        end={
          <>
            <Button onClick={back}>{t("v2.common.cancel")}</Button>
            <Button kind="primary" disabled={busy || !provisioned} onClick={() => void submit()} testId="v2-train-submit">
              {busy ? t("skillLab.eval.wizard.submitting") : t("skillLab.train.wizard.submit")}
            </Button>
          </>
        }
      />
      {!provisioned && <Alert tone="warn">{t("skillLab.eval.wizard.blocked")}</Alert>}
      {error && <Alert tone="error">{error}</Alert>}
      <Card title={t("skillLab.train.wizard.field.skill")} sub={t("skillLab.train.wizard.sub")}>
        <SkillSourcePicker value={source} onChange={setSource} testId="v2-train-source" uploadNote={t("skillLab.train.wizard.uploadNote")} />
        {candidateFiles.length > 0 && (
          <div style={{ marginTop: 16 }} data-testid="v2-train-trainable">
            <h3 className="v2-sub-title">{t("skillLab.train.wizard.field.trainableFiles")}</h3>
            <div className="v2-checks">
              {candidateFiles.map((file) => (
                <label key={file} className="v2-check">
                  <input
                    type="checkbox"
                    checked={trainableFiles.includes(file)}
                    onChange={() =>
                      setTrainableFiles((prev) => (prev.includes(file) ? prev.filter((f) => f !== file) : [...prev, file]))
                    }
                  />
                  <span className="mono">{file}</span>
                </label>
              ))}
            </div>
            <p className="v2-muted v2-skilllab-hint">
              {t("skillLab.train.wizard.hint.trainableFiles", { n: trainableFiles.length })}
            </p>
          </div>
        )}
      </Card>
      <Card title={t("v2.skillLab.trainLoop")}>
        <div className="v2-form cols-2">
          <Field
            label={t("skillLab.eval.wizard.field.taskset")}
            required
            hint={
              selected === null
                ? !tasksets.loading && rows.length === 0
                  ? t("skillLab.eval.wizard.noTasksets")
                  : undefined
                : selected.mode === "single"
                  ? t("skillLab.train.wizard.autoSplit", { train: counts.train, val: counts.val })
                  : t("skillLab.train.wizard.splitCounts", { train: counts.train, val: counts.val, test: selected.counts.test ?? 0 })
            }
          >
            <select className="v2-select" value={tasksetId} onChange={(e) => setTasksetId(e.target.value)} data-testid="v2-train-taskset">
              <option value="">{t("v2.common.choose")}</option>
              {rows.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.name} ({t(`skillLab.tasksets.mode.${row.mode}`)})
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("skillLab.train.wizard.field.gateMetric")} hint={t("skillLab.train.wizard.hint.gateMetric")}>
            <select
              className="v2-select"
              value={gateMetric}
              onChange={(e) => setGateMetric(e.target.value as SkillLabGateMetric)}
              data-testid="v2-train-gate"
            >
              {GATE_METRICS.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </Field>
          <NumberField label={t("skillLab.train.wizard.field.epochs")} hint={t("skillLab.train.wizard.hint.epochs", { min: 1, max: 10 })} value={epochs} onChange={setEpochs} min={1} max={10} testId="v2-train-epochs" />
          <NumberField label={t("skillLab.train.wizard.field.learningRate")} hint={t("skillLab.train.wizard.hint.learningRate", { min: 1, max: 16 })} value={learningRate} onChange={setLearningRate} min={1} max={16} />
        </div>
        {selected !== null && (
          <div style={{ marginTop: 12 }} data-testid="v2-train-estimate">
            <Alert tone="warn">{t("skillLab.train.wizard.estimate", { n: estimate })}</Alert>
          </div>
        )}
      </Card>
      <Card title={t("v2.skillLab.models")}>
        <ModelFields
          status={status}
          testId="v2-train"
          backend={backend}
          setBackend={setBackend}
          targetModel={targetModel}
          setTargetModel={setTargetModel}
          judge={{
            model: judgeModel,
            setModel: setJudgeModel,
            mode: judgeMode,
            setMode: setJudgeMode,
            label: t("skillLab.train.wizard.field.judgeModel"),
          }}
        />
      </Card>
      <Card title={t("skillLab.eval.wizard.advanced")}>
        <div className="v2-form cols-2">
          <NumberField label={t("skillLab.train.wizard.field.workers")} hint={t("skillLab.train.wizard.hint.workers", { min: 1, max: 8 })} value={workers} onChange={setWorkers} min={1} max={8} />
          <NumberField label={t("skillLab.train.wizard.field.timeout")} hint={t("skillLab.train.wizard.hint.timeout", { min: 60, max: 3600 })} value={timeout} onChange={setTimeoutSeconds} min={60} max={3600} />
          <NumberField label={t("skillLab.train.wizard.field.limit")} hint={t("skillLab.train.wizard.hint.limit", { min: 0, max: 10000 })} value={limit} onChange={setLimit} min={0} max={10000} />
        </div>
      </Card>
    </>
  );
}

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage, type SkillLabJudgeMode, type SkillLabStatus, type SkillLabTargetBackend } from "../../../lib/api";
import { canSubmitJobs, evalSplitsOf } from "../../../lib/skillLab";
import { Alert, Button, Card, Field, FlowHeader, Segmented } from "../../ui";
import { ModelFields } from "./ModelFields";
import { SkillSourcePicker } from "./SkillPicker";
import { initialSource, skillSourceOf, type SourceState, useTasksets } from "./state";

/** Bounded integer inputs of the eval / train wizards. */
export function NumberField({
  label,
  hint,
  value,
  onChange,
  min,
  max,
  testId,
}: {
  label: string;
  hint: string;
  value: number;
  onChange: (n: number) => void;
  min: number;
  max: number;
  testId?: string;
}) {
  return (
    <Field label={label} hint={hint}>
      <input
        className="v2-input"
        type="number"
        min={min}
        max={max}
        value={value}
        data-testid={testId}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </Field>
  );
}

/**
 * New evaluation job (`?tab=eval&view=new[&record=][&taskset=]`): skill ×
 * task set (× split) × models, run on the exec worker. Model fields prefill
 * from the platform config, so an operator override is what the wizard shows
 * — and a typed value is never overwritten.
 */
export function EvalWizard({ status }: { status: SkillLabStatus | null }) {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tasksets = useTasksets("skill-lab-tasksets:eval-wizard");
  const [source, setSource] = useState<SourceState>(() => initialSource(params.get("record")));
  const [tasksetId, setTasksetId] = useState(params.get("taskset") ?? "");
  const [split, setSplit] = useState("");
  const [backend, setBackend] = useState<SkillLabTargetBackend>("claude_code_exec");
  const [judgeMode, setJudgeMode] = useState<SkillLabJudgeMode>("auto");
  const [targetModel, setTargetModel] = useState("");
  const [judgeModel, setJudgeModel] = useState("");
  const [workers, setWorkers] = useState(2);
  const [timeout, setTimeoutSeconds] = useState(600);
  const [limit, setLimit] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const provisioned = canSubmitJobs(status);

  const rows = useMemo(() => tasksets.data ?? [], [tasksets.data]);
  useEffect(() => {
    if (rows.length) setTasksetId((prev) => prev || rows[0].id);
  }, [rows]);
  useEffect(() => {
    if (status === null) return;
    setTargetModel((prev) => prev || status.default_target_model);
    setJudgeModel((prev) => prev || status.default_judge_model);
  }, [status]);

  const selected = rows.find((row) => row.id === tasksetId) ?? null;
  const splits = useMemo(() => (selected ? evalSplitsOf(selected) : []), [selected]);
  // Follow the task set: a split carried over from the previous one would be refused (422).
  useEffect(() => {
    setSplit(splits[0] ?? "");
  }, [splits]);

  const back = () => setParams({ tab: "eval" });

  const submit = async () => {
    setError(null);
    const skillSource = skillSourceOf(source);
    if (skillSource === null) {
      setError(t("skillLab.eval.wizard.err.noSkill"));
      return;
    }
    if (!tasksetId) {
      setError(t("skillLab.eval.wizard.err.noTaskset"));
      return;
    }
    setBusy(true);
    try {
      const job = await api.skillLabJobCreate({
        type: "eval",
        skill_source: skillSource,
        taskset_id: tasksetId,
        ...(split ? { split } : {}),
        params: {
          target_backend: backend,
          target_model: targetModel.trim(),
          judge_model: judgeModel.trim(),
          judge_mode: judgeMode,
          workers,
          timeout,
          limit,
        },
      });
      setParams({ tab: "eval", view: "detail", id: job.id });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <FlowHeader
        title={t("skillLab.eval.wizard.title")}
        onBack={back}
        end={
          <>
            <Button onClick={back}>{t("v2.common.cancel")}</Button>
            <Button kind="primary" disabled={busy || !provisioned} onClick={() => void submit()} testId="v2-eval-submit">
              {busy ? t("skillLab.eval.wizard.submitting") : t("skillLab.eval.wizard.submit")}
            </Button>
          </>
        }
      />
      {!provisioned && <Alert tone="warn">{t("skillLab.eval.wizard.blocked")}</Alert>}
      {error && <Alert tone="error">{error}</Alert>}
      <Card title={t("skillLab.eval.wizard.field.skill")} sub={t("skillLab.eval.wizard.sub")}>
        <SkillSourcePicker value={source} onChange={setSource} testId="v2-eval-source" uploadNote={t("skillLab.eval.wizard.uploadNote")} />
      </Card>
      <Card title={t("skillLab.eval.wizard.field.taskset")}>
        <div className="v2-form cols-2">
          <Field
            label={t("skillLab.eval.wizard.field.taskset")}
            required
            hint={!tasksets.loading && rows.length === 0 ? t("skillLab.eval.wizard.noTasksets") : undefined}
          >
            <select className="v2-select" value={tasksetId} onChange={(e) => setTasksetId(e.target.value)} data-testid="v2-eval-taskset">
              <option value="">{t("v2.common.choose")}</option>
              {rows.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.name} ({t(`skillLab.tasksets.mode.${row.mode}`)})
                </option>
              ))}
            </select>
          </Field>
          {splits.length > 0 && (
            <Field label={t("v2.skillLab.split")} hint={t("v2.skillLab.splitHint")}>
              <Segmented
                value={split}
                onChange={setSplit}
                options={splits.map((s) => ({ value: s, label: `${s} (${selected?.counts[s] ?? 0})` }))}
              />
            </Field>
          )}
        </div>
      </Card>
      <Card title={t("v2.skillLab.models")}>
        <ModelFields
          status={status}
          testId="v2-eval"
          backend={backend}
          setBackend={setBackend}
          targetModel={targetModel}
          setTargetModel={setTargetModel}
          judge={{ model: judgeModel, setModel: setJudgeModel, mode: judgeMode, setMode: setJudgeMode }}
        />
      </Card>
      <Card title={t("skillLab.eval.wizard.advanced")}>
        <div className="v2-form cols-2">
          <NumberField label={t("skillLab.eval.wizard.field.workers")} hint={t("skillLab.eval.wizard.hint.workers", { min: 1, max: 8 })} value={workers} onChange={setWorkers} min={1} max={8} testId="v2-eval-workers" />
          <NumberField label={t("skillLab.eval.wizard.field.timeout")} hint={t("skillLab.eval.wizard.hint.timeout", { min: 60, max: 3600 })} value={timeout} onChange={setTimeoutSeconds} min={60} max={3600} />
          <NumberField label={t("skillLab.eval.wizard.field.limit")} hint={t("skillLab.eval.wizard.hint.limit", { min: 0, max: 10000 })} value={limit} onChange={setLimit} min={0} max={10000} />
        </div>
      </Card>
    </>
  );
}

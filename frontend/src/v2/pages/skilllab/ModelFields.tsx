import { useTranslation } from "react-i18next";

import type { SkillLabJudgeMode, SkillLabStatus, SkillLabTargetBackend } from "../../../lib/api";
import {
  BACKEND_LABELS,
  backendsOf,
  JUDGE_MODEL_SUGGESTIONS,
  judgeReadiness,
  modelDefault,
} from "../../../lib/skillLab";
import { Field } from "../../ui";

export interface JudgeFields {
  model: string;
  setModel: (m: string) => void;
  mode: SkillLabJudgeMode;
  setMode: (m: SkillLabJudgeMode) => void;
  /** label override (training: the optimizer and the judge are one model) */
  label?: string;
}

/**
 * Exec backend + model fields shared by the eval / train / taskgen wizards.
 * Switching backends swaps the target-model prefill only while the field still
 * holds the other backend's default (upstream `applyBackend` semantics) — a
 * hand-typed model is never overwritten.
 */
export function ModelFields({
  status,
  testId,
  backend,
  setBackend,
  targetModel,
  setTargetModel,
  judge,
}: {
  status: SkillLabStatus | null;
  testId: string;
  backend: SkillLabTargetBackend;
  setBackend: (b: SkillLabTargetBackend) => void;
  targetModel: string;
  setTargetModel: (m: string) => void;
  judge?: JudgeFields;
}) {
  const { t } = useTranslation();
  const judgeModes: SkillLabJudgeMode[] = status?.judge_modes?.length ? status.judge_modes : ["auto", "chat", "agentic"];
  const readiness = judgeReadiness(status, judge?.model ?? "");

  const applyBackend = (next: SkillLabTargetBackend) => {
    if (next === backend) return;
    const previousDefault = modelDefault(status, backend);
    setBackend(next);
    if (!targetModel.trim() || targetModel === previousDefault) setTargetModel(modelDefault(status, next));
  };

  return (
    <div className="v2-form cols-2">
      <Field label={t("skillLab.backend.field")} hint={t("skillLab.backend.hint")}>
        <select
          className="v2-select"
          value={backend}
          data-testid={`${testId}-backend`}
          onChange={(e) => applyBackend(e.target.value as SkillLabTargetBackend)}
        >
          {backendsOf(status).map((option) => (
            <option key={option} value={option}>
              {BACKEND_LABELS[option] ?? option}
            </option>
          ))}
        </select>
      </Field>
      <Field
        label={t("skillLab.eval.wizard.field.targetModel")}
        hint={t(backend === "codex_exec" ? "skillLab.backend.targetHintCodex" : "skillLab.backend.targetHintClaude")}
      >
        <input
          className="v2-input mono"
          value={targetModel}
          data-testid={`${testId}-target-model`}
          onChange={(e) => setTargetModel(e.target.value)}
        />
      </Field>
      {judge && (
        <>
          <Field label={judge.label ?? t("skillLab.eval.wizard.field.judgeModel")} hint={t("skillLab.backend.judgeHint")}>
            <input
              className="v2-input mono"
              value={judge.model}
              list={`${testId}-judge-models`}
              data-testid={`${testId}-judge-model`}
              onChange={(e) => judge.setModel(e.target.value)}
            />
            <datalist id={`${testId}-judge-models`}>
              {JUDGE_MODEL_SUGGESTIONS.map((model) => (
                <option key={model} value={model} />
              ))}
            </datalist>
          </Field>
          <Field
            label={t("skillLab.backend.judgeMode")}
            hint={
              judge.mode !== "chat" && !readiness.ready ? (
                <span className="v2-skilllab-warn">{t(readiness.unreadyKey)}</span>
              ) : (
                t(`skillLab.backend.judgeModeHint.${judge.mode}`)
              )
            }
          >
            <select
              className="v2-select"
              value={judge.mode}
              data-testid={`${testId}-judge-mode`}
              onChange={(e) => judge.setMode(e.target.value as SkillLabJudgeMode)}
            >
              {judgeModes.map((option) => (
                <option key={option} value={option}>
                  {t(`skillLab.backend.judgeModeOption.${option}`)}
                  {option !== "chat" && !readiness.ready ? " ⚠" : ""}
                </option>
              ))}
            </select>
          </Field>
        </>
      )}
    </div>
  );
}

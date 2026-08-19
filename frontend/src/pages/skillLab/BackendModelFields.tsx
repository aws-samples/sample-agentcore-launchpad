import { useTranslation } from "react-i18next";
import type { SkillLabJudgeMode, SkillLabStatus, SkillLabTargetBackend } from "../../lib/api";

/** Upstream studio's fixed backend labels — the values are API contract. */
const BACKEND_LABELS: Record<SkillLabTargetBackend, string> = {
  claude_code_exec: "claude_code_exec — Claude Code CLI",
  codex_exec: "codex_exec — Codex CLI",
};

/**
 * Judge suggestions are Converse inference-profile ids (`us.`/`global.`
 * prefixed) — the bedrock_chat judge calls Converse directly, and bare model
 * ids like `openai.gpt-5.6-sol` are rejected with "use an inference profile".
 * The agentic judge routes by family: openai.* → host codex CLI, else claude.
 */
const JUDGE_MODEL_SUGGESTIONS = [
  "us.openai.gpt-5.6-sol",
  "global.anthropic.claude-opus-5",
  "us.anthropic.claude-sonnet-5",
];

/** Mirrors backend runner.judge_exec_route's family test. */
const isOpenAiJudge = (model: string) => /^((us|eu|apac|global)\.)?openai\./.test(model);

function defaultFor(status: SkillLabStatus | null, backend: SkillLabTargetBackend): string {
  if (status === null) return "";
  return backend === "codex_exec" ? status.default_codex_target_model : status.default_target_model;
}

/**
 * Shared exec-backend + model fields for the eval/train (and taskgen) wizards.
 *
 * Switching backends swaps the target-model prefill only while the field still
 * holds the other backend's default (upstream `applyBackend` semantics) — a
 * hand-typed model is never overwritten.
 */
export function BackendModelFields({
  status,
  idPrefix,
  backend,
  setBackend,
  targetModel,
  setTargetModel,
  judgeModel,
  setJudgeModel,
  judgeMode,
  setJudgeMode,
}: {
  status: SkillLabStatus | null;
  /** data-testid prefix, e.g. "eval" → eval-param-targetModel */
  idPrefix: string;
  backend: SkillLabTargetBackend;
  setBackend: (b: SkillLabTargetBackend) => void;
  targetModel: string;
  setTargetModel: (m: string) => void;
  judgeModel: string;
  setJudgeModel: (m: string) => void;
  judgeMode: SkillLabJudgeMode;
  setJudgeMode: (m: SkillLabJudgeMode) => void;
}) {
  const { t } = useTranslation();
  const backends = status?.target_backends?.length
    ? status.target_backends
    : (Object.keys(BACKEND_LABELS) as SkillLabTargetBackend[]);
  const judgeModes: SkillLabJudgeMode[] = status?.judge_modes?.length
    ? status.judge_modes
    : ["auto", "chat", "agentic"];
  // Without the host sandbox launcher — or, for an openai-family judge model,
  // without the host codex CLI the agentic judge routes to — auto/agentic
  // still run, but binary-artifact tasks fail closed. Warn, don't hide.
  const codexJudgeMissing = isOpenAiJudge(judgeModel) && status?.judge_codex_ready === false;
  const agenticReady = status?.agentic_judge_ready !== false && !codexJudgeMissing;

  const applyBackend = (next: SkillLabTargetBackend) => {
    if (next === backend) return;
    const previousDefault = defaultFor(status, backend);
    setBackend(next);
    if (!targetModel.trim() || targetModel === previousDefault) {
      setTargetModel(defaultFor(status, next));
    }
  };

  return (
    <>
      <div className="field">
        <label>{t("skillLab.backend.field")}</label>
        <select
          className="input"
          value={backend}
          data-testid={`${idPrefix}-param-targetBackend`}
          onChange={(e) => applyBackend(e.target.value as SkillLabTargetBackend)}
        >
          {backends.map((option) => (
            <option key={option} value={option} style={{ background: "#141816" }}>
              {BACKEND_LABELS[option] ?? option}
            </option>
          ))}
        </select>
        <span className="mono dim" style={{ fontSize: 10.5 }}>
          {t("skillLab.backend.hint")}
        </span>
      </div>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <div className="field" style={{ flex: 1, minWidth: 240 }}>
          <label>{t("skillLab.eval.wizard.field.targetModel")}</label>
          <input
            className="input mono"
            value={targetModel}
            data-testid={`${idPrefix}-param-targetModel`}
            onChange={(e) => setTargetModel(e.target.value)}
          />
          <span className="mono dim" style={{ fontSize: 10.5 }}>
            {t(
              backend === "codex_exec"
                ? "skillLab.backend.targetHintCodex"
                : "skillLab.backend.targetHintClaude",
            )}
          </span>
        </div>
        <div className="field" style={{ flex: 1, minWidth: 240 }}>
          <label>{t("skillLab.eval.wizard.field.judgeModel")}</label>
          <input
            className="input mono"
            value={judgeModel}
            list="skill-lab-judge-models"
            data-testid={`${idPrefix}-param-judgeModel`}
            onChange={(e) => setJudgeModel(e.target.value)}
          />
          <datalist id="skill-lab-judge-models">
            {JUDGE_MODEL_SUGGESTIONS.map((model) => (
              <option key={model} value={model} />
            ))}
          </datalist>
          <span className="mono dim" style={{ fontSize: 10.5 }}>
            {t("skillLab.backend.judgeHint")}
          </span>
        </div>
        <div className="field" style={{ minWidth: 170 }}>
          <label>{t("skillLab.backend.judgeMode")}</label>
          <select
            className="input"
            value={judgeMode}
            data-testid={`${idPrefix}-param-judgeMode`}
            onChange={(e) => setJudgeMode(e.target.value as SkillLabJudgeMode)}
          >
            {judgeModes.map((option) => (
              <option key={option} value={option} style={{ background: "#141816" }}>
                {t(`skillLab.backend.judgeModeOption.${option}`)}
                {option !== "chat" && !agenticReady ? " ⚠" : ""}
              </option>
            ))}
          </select>
          <span className="mono dim" style={{ fontSize: 10.5 }}>
            {judgeMode !== "chat" && !agenticReady
              ? t(
                  codexJudgeMissing
                    ? "skillLab.backend.judgeCodexUnready"
                    : "skillLab.backend.judgeModeUnready",
                )
              : t(`skillLab.backend.judgeModeHint.${judgeMode}`)}
          </span>
        </div>
      </div>
    </>
  );
}

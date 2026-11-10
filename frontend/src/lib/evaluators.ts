import type { TFunction } from "i18next";

import { customEvaluatorName } from "./evaluator-names";
import { ALLOWED_MODEL_IDS, MODEL_CATALOG } from "./models";

// Localized display names for evaluators. Builtins translate through the
// evalPage.evaluatorNames.<Name> locale block, third-party managed evaluators
// (ThirdParty.<Provider>.<Metric>) through evalPage.thirdPartyNames.<Metric>
// (both falling back to the bare id segment); custom evaluators show their user-given
// name from the evaluator catalogue (N17-obs429 — `useEvaluatorNames` loads it), and the
// id only when the catalogue has no row for it (a deleted evaluator).
export function evaluatorLabel(t: TFunction, id: string): string {
  if (id.startsWith("Builtin.")) {
    const bare = id.slice("Builtin.".length);
    return t(`evalPage.evaluatorNames.${bare}`, { defaultValue: bare });
  }
  if (id.startsWith("ThirdParty.")) {
    const metric = id.split(".").slice(2).join(".");
    if (metric) return t(`evalPage.thirdPartyNames.${metric}`, { defaultValue: metric });
  }
  return customEvaluatorName(id) ?? id;
}

// Penalty evaluators: Refusal scores "Yes" = 1.0 when the agent refused, so a lower
// mean is the better arm. Harmfulness / Stereotyping are not: the live scale is
// "Not Harmful" / "Not Stereotyping" = 1.0. DeepEval PIILeakage is NOT
// one: it scores non-PII / all statements, so 1.0 means no leakage. Mirrors
// LOWER_IS_BETTER_EVALUATORS in backend/app/evaluation/agentcore_eval.py — the
// backend annotates each A/B metric with `polarity`, and this list is only the
// fallback for verdict artifacts stored before it did.
const LOWER_IS_BETTER = new Set([
  "Builtin.Refusal",
  "ThirdParty.DeepEval.Bias",
  "ThirdParty.DeepEval.Toxicity",
]);

// +1 when a higher mean is the better arm, -1 when a lower mean is. Takes the id
// as the metric carries it (`Builtin.Refusal`); custom judges are +1 because AWS
// exposes no direction for them — and a custom judge merely *named* "Refusal" is
// not the built-in one.
export function evaluatorPolarity(id: string): number {
  return LOWER_IS_BETTER.has(id) ? -1 : 1;
}

/** N17-obs425: the oriented score a row must reach to count as passed — mirrors
 *  `evaluators_local.PASS_THRESHOLDS` (the catalog's `pass_threshold`): 0.7 by default, 1 for
 *  two-label evaluators, trajectory matches, Correctness (Perfectly Correct only),
 *  SkillInstructionFollowing (pass = Fully Followed only; Mostly Followed 0.75 fails).
 *  Faithfulness and Coherence (Completely Yes only). Custom evaluators keep the default. */
export const DEFAULT_PASS_THRESHOLD = 0.7;
export const PASS_THRESHOLDS: Record<string, number> = {
  // user decision 2026-10-25: Completely Yes only (hard calibration tier c76c90a4a420b75b-hard)
  "Builtin.Faithfulness": 1,
  // lead decision 2026-10-25, the same rule: Completely Yes only (calibration c76c90a4a420b75b)
  "Builtin.Coherence": 1,
  "Builtin.Correctness": 1,
  "Builtin.GoalSuccessRate": 1,
  "Builtin.InstructionFollowing": 1,
  "Builtin.Refusal": 1,
  "Builtin.Harmfulness": 1,
  "Builtin.Stereotyping": 1,
  "Builtin.ToolSelectionAccuracy": 1,
  "Builtin.ToolParameterAccuracy": 1,
  "Builtin.SkillSelectionAccuracy": 1,
  "Builtin.SkillInstructionFollowing": 1,
  "Builtin.TrajectoryExactOrderMatch": 1,
  "Builtin.TrajectoryInOrderMatch": 1,
  "Builtin.TrajectoryAnyOrderMatch": 1,
};

/** The pass bar of an evaluator (bare id or ARN). */
export function passThreshold(evaluatorId: string): number {
  const bare = (evaluatorId || "").split("/").pop() ?? "";
  return PASS_THRESHOLDS[bare] ?? DEFAULT_PASS_THRESHOLD;
}

/** Float slack: 0.833 stored as 0.8329999 still reaches a 0.833 bar. */
const PASS_EPSILON = 1e-9;

/** Whether a polarity-ORIENTED 0..1 score reaches the evaluator's pass bar. */
export function passesBar(oriented: number, evaluatorId: string): boolean {
  return oriented >= passThreshold(evaluatorId) - PASS_EPSILON;
}

/** Polarity-aware colour for one 0..1 score (shared by SCORE NOW, the online
 *  results and the run results): a penalty evaluator is good when LOW, so its
 *  thresholds invert. */
export function scoreColor(score: number, evaluatorId: string): string {
  const oriented = evaluatorPolarity(evaluatorId) < 0 ? 1 - score : score;
  return passesBar(oriented, evaluatorId)
    ? "var(--good)"
    : oriented >= 0.4
      ? "var(--warn)"
      : "var(--crit-text)";
}

/** Bedrock models offered as the judge of a custom LLM evaluator — the CN model
 *  catalog (GLM 5.3 first = the backend default, then Kimi K3), mirroring the
 *  backend ALLOWED_MODEL_IDS (Launchpad-CN constraint C3). */
export const JUDGE_MODEL_OPTIONS: string[] = ALLOWED_MODEL_IDS; // live: grows with /api/capabilities (N-R6)
void MODEL_CATALOG;

export type EvaluatorLevel = "TOOL_CALL" | "TRACE" | "SESSION";

/** Custom evaluator names: the backend `JudgeCreate.name` pattern (N17-obs312). */
export const EVALUATOR_NAME_RE = /^[a-zA-Z][a-zA-Z0-9_]{0,47}$/;

/** Prompt placeholders the engine fills per evaluation level: `core` come from the
 *  session's own trace (so they work online too), `groundTruth` need a dataset with
 *  expected values — the backend `judge_placeholders.GROUND_TRUTH_PLACEHOLDERS`; the
 *  actual tool trajectory is measured from the spans (N17-obs311) — and `skill` apply
 *  to skill-invocation tool calls. A judge prompt needs at least one. */
export const LEVEL_PLACEHOLDERS: Record<
  EvaluatorLevel,
  { core: string[]; groundTruth: string[]; skill?: string[] }
> = {
  TRACE: {
    core: ["{context}", "{assistant_turn}"],
    groundTruth: ["{expected_response}"],
  },
  SESSION: {
    core: ["{context}", "{available_tools}", "{actual_tool_trajectory}"],
    groundTruth: ["{assertions}", "{expected_tool_trajectory}"],
  },
  TOOL_CALL: {
    core: ["{context}", "{available_tools}", "{tool_turn}"],
    groundTruth: [],
    skill: ["{invoked_skill}", "{skill_content}", "{available_skills}", "{user_message}"],
  },
};

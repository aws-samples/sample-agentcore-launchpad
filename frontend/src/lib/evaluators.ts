import type { TFunction } from "i18next";

// Localized display names for evaluators. Builtins translate through the
// evalPage.evaluatorNames.<Name> locale block, third-party managed evaluators
// (ThirdParty.<Provider>.<Metric>) through evalPage.thirdPartyNames.<Metric>
// (both falling back to the bare id segment); custom judges keep their
// user-given name untouched.
export function evaluatorLabel(t: TFunction, id: string): string {
  if (id.startsWith("Builtin.")) {
    const bare = id.slice("Builtin.".length);
    return t(`evalPage.evaluatorNames.${bare}`, { defaultValue: bare });
  }
  if (id.startsWith("ThirdParty.")) {
    const metric = id.split(".").slice(2).join(".");
    if (metric) return t(`evalPage.thirdPartyNames.${metric}`, { defaultValue: metric });
  }
  return id;
}

// Penalty evaluators: the judge scores "Yes"/"Harmful"/"Stereotyping" when the
// response is BAD, so a lower mean is the better arm. Mirrors
// LOWER_IS_BETTER_EVALUATORS in backend/app/evaluation/agentcore_eval.py — the
// backend annotates each A/B metric with `polarity`, and this list is only the
// fallback for verdict artifacts stored before it did.
const LOWER_IS_BETTER = new Set([
  "Builtin.Refusal",
  "Builtin.Harmfulness",
  "Builtin.Stereotyping",
  "ThirdParty.DeepEval.Bias",
  "ThirdParty.DeepEval.Toxicity",
  "ThirdParty.DeepEval.PIILeakage",
]);

// +1 when a higher mean is the better arm, -1 when a lower mean is. Takes the id
// as the metric carries it (`Builtin.Refusal`); custom judges are +1 because AWS
// exposes no direction for them — and a custom judge merely *named* "Refusal" is
// not the built-in one.
export function evaluatorPolarity(id: string): number {
  return LOWER_IS_BETTER.has(id) ? -1 : 1;
}

/** Polarity-aware colour for one 0..1 score (shared by SCORE NOW, the online
 *  results and the run results): a penalty evaluator is good when LOW, so its
 *  thresholds invert. */
export function scoreColor(score: number, evaluatorId: string): string {
  const oriented = evaluatorPolarity(evaluatorId) < 0 ? 1 - score : score;
  return oriented >= 0.7
    ? "var(--good)"
    : oriented >= 0.4
      ? "var(--warn)"
      : "var(--crit-text)";
}

/** Bedrock models offered as the judge of a custom LLM evaluator (the backend
 *  default, `global.anthropic.claude-sonnet-5`, first). */
export const JUDGE_MODEL_OPTIONS = [
  "global.anthropic.claude-sonnet-5",
  "global.anthropic.claude-sonnet-4-6",
  "global.anthropic.claude-haiku-4-5-20251001-v1:0",
  "global.anthropic.claude-opus-4-8",
  "global.anthropic.claude-opus-4-6-v1",
  "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
  "global.amazon.nova-2-lite-v1:0",
  "us.amazon.nova-pro-v1:0",
];

export type EvaluatorLevel = "TOOL_CALL" | "TRACE" | "SESSION";

/** Prompt placeholders AgentCore fills per evaluation level: `core` are always
 *  available, `groundTruth` need a dataset with expected values, `skill` apply
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
    core: ["{context}", "{available_tools}"],
    groundTruth: ["{assertions}", "{expected_tool_trajectory}", "{actual_tool_trajectory}"],
  },
  TOOL_CALL: {
    core: ["{context}", "{available_tools}", "{tool_turn}"],
    groundTruth: [],
    skill: ["{invoked_skill}", "{skill_content}", "{available_skills}", "{user_message}"],
  },
};

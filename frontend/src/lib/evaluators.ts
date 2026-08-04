import type { TFunction } from "i18next";

// Localized display names for evaluators. Builtins translate through the
// evalPage.evaluatorNames.<Name> locale block (falling back to the bare id
// segment); custom judges keep their user-given name untouched.
export function evaluatorLabel(t: TFunction, id: string): string {
  if (!id.startsWith("Builtin.")) return id;
  const bare = id.slice("Builtin.".length);
  return t(`evalPage.evaluatorNames.${bare}`, { defaultValue: bare });
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
]);

// +1 when a higher mean is the better arm, -1 when a lower mean is. Takes the id
// as the metric carries it (`Builtin.Refusal`); custom judges are +1 because AWS
// exposes no direction for them — and a custom judge merely *named* "Refusal" is
// not the built-in one.
export function evaluatorPolarity(id: string): number {
  return LOWER_IS_BETTER.has(id) ? -1 : 1;
}

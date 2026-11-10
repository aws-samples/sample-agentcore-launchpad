/**
 * Coded validation errors (N17-obs296): the backend keeps every validation error as an
 * operator-readable English sentence and adds `{code, params, message}`
 * (`backend/app/assistant/validation_codes.py`). Known codes render in the member's
 * language; the English sentence stays available as the expandable detail. An unknown
 * code — or a payload from an older backend that carries only strings — renders the
 * sentence itself.
 */
import type { TFunction } from "i18next";

import type { ValidationErrorDetail } from "./api";

/** backend code → i18n key under `assistantPage.validationError` */
const KEYS: Record<string, string> = {
  "evaluation_plan.reference_missing": "referenceMissing",
  "evaluation_plan.trace_turn_missing_response": "traceTurnMissingResponse",
  "evaluation_plan.evaluator_partial_routing": "evaluatorPartialRouting",
};

/** One error per sentence: the coded detail when the payload carries a matching one. */
export function errorDetails(
  messages: string[],
  details?: ValidationErrorDetail[] | null,
): ValidationErrorDetail[] {
  return messages.map((message, i) => {
    const d = details?.[i];
    return d && d.message === message ? d : { code: "proposal.validation_error", params: {}, message };
  });
}

const list = (v: unknown): string =>
  Array.isArray(v) ? v.map(String).join(", ") : String(v ?? "");

/** The localized sentence, or `null` when the code has no translation. */
export function localizedError(t: TFunction, detail: ValidationErrorDetail): string | null {
  const key = KEYS[detail.code];
  if (!key) return null;
  const p = detail.params ?? {};
  return t(`assistantPage.validationError.${key}`, {
    evaluator: String(p.evaluator ?? ""),
    scenario: String(p.scenario ?? ""),
    fields: list(p.fields),
    goldenTests: list(p.golden_tests),
  });
}

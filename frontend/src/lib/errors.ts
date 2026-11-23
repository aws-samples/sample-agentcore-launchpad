// The console's one wording of an error (storyline-ux §4, SG-5): V2 and V3 alike show
// a backend code as its `apiErrors.<code>` copy in the active locale — the backend's
// own `message` is operator-facing log English, kept only where no copy exists (and,
// for `aws.*` and `DETAIL_CODES`, appended after the copy: only those words say which
// parameter, line or item).
// `backend/tests/test_error_copy.py` fails when a code the backend raises has no copy.
import { ApiError, errorMessage, localizedMessage, messageWithRaw } from "./api";

/**
 * Codes whose backend message carries the specifics of the failure — the line of a
 * JSONL file, the validator's list, the converter's reason — that the generic copy
 * cannot say. The console shows the copy, then that message (SG-5b).
 */
export const DETAIL_CODES: ReadonlySet<string> = new Set([
  "agent.convert_failed",
  "agent.mount_failed",
  "assistant.preparation_invalid",
  "cost_tags.invalid",
  "custom_tool.invalid",
  "dataset.invalid_item",
  "dataset.invalid_jsonl",
  "governance.rate_limit_invalid",
  "identity.invalid_openapi",
  "lambda_tool.invalid_schema",
  "memory.invalid_strategy",
  "registry.skill_invalid",
  "run.invalid_insight",
  "skill_lab.taskgen_bad_selection",
  "skill_lab.taskset_invalid",
  "skill_lab.validator_error",
  "sso.invalid_mappings",
  "users.invalid_permissions",
  "users.invalid_workspaces",
]);

/**
 * An envelope's `{code, message}` as the console says it — for the places that get
 * the pair without an `ApiError` (an SSE `error` event, a job's stored failure).
 * No code ⇒ the message as sent.
 */
export function envelopeText(code: string | null | undefined, message: string): string {
  return code ? messageWithRaw(code, message) : message;
}

/**
 * Any caught error as the console says it, re-read in the current language (an
 * `ApiError` was worded when it arrived). A fetch-level failure gets the network copy.
 */
export function errorText(err: unknown): string {
  if (err instanceof ApiError) return envelopeText(err.code, err.rawMessage);
  return errorMessage(err);
}

/** The headline alone (no AWS text), for a title line above the details. */
export function errorTitle(err: unknown): string {
  if (err instanceof ApiError) return localizedMessage(err.code, err.rawMessage);
  return errorMessage(err);
}

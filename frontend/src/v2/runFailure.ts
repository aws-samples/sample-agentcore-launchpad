// N17-obs450: a run's failure reason in the console language. The run payloads carry the
// stored `error` text plus `error_code` / `error_params` (`backend/app/evaluation/run_failure.py`
// `classify`); the console words the code and keeps the raw text for a tooltip / detail.
import type { TFunction } from "i18next";

/** Every code `run_failure.classify` returns (`test_run_failure_codes.py` pins this list). */
export const RUN_FAILURE_CODES = [
  "stopped_by_operator",
  "job_lost",
  "still_running",
  "all_records_failed",
  "some_records_errored",
  "app_error",
  "error",
] as const;

export interface RunFailureFields {
  error?: string | null;
  error_code?: string | null;
  error_params?: Record<string, unknown> | null;
}

/** The reason worded from `error_code` + `error_params`; the raw `error` for an older
 *  payload, an unknown code or text in no known shape; null without an error. */
export function runFailureText(run: RunFailureFields | null | undefined, t: TFunction): string | null {
  if (!run?.error) return null;
  const code = run.error_code;
  if (!code || !(RUN_FAILURE_CODES as readonly string[]).includes(code)) return run.error;
  const params: Record<string, unknown> = { ...(run.error_params ?? {}) };
  for (const key of ["first_class", "failure_class"]) {
    if (typeof params[key] === "string") params[key] = t(`v2.unitCoverage.class.${params[key]}`);
  }
  if (typeof params.unit === "string") params.unit = t(`runFailure.unit.${params.unit}`);
  return t(`runFailure.code.${code}`, params);
}

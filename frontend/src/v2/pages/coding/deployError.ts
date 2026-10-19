import { ApiError, errorMessage } from "../../../lib/api";

type Translate = (key: string, options?: Record<string, unknown>) => string;

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/**
 * The deploy modal's copy for a failed deploy. `422 coding.deploy_failed` names why
 * in `detail.reason` (backend `app/coding/deploy.py`): `entrypoint_missing` (+
 * `entrypoint`, `candidates` — the Python files at the project root),
 * `dockerfile_missing` or `invalid_spec` (+ `errors`). The generic
 * `apiErrors.coding.deploy_failed` copy cannot say which, so the person would not
 * know what to fix. Every other failure keeps the shared localized message.
 */
export function deployFailureText(err: unknown, t: Translate): string {
  if (err instanceof ApiError && err.code === "coding.deploy_failed" && err.detail && typeof err.detail === "object") {
    const d = err.detail as Record<string, unknown>;
    if (d.reason === "entrypoint_missing") {
      const entrypoint = typeof d.entrypoint === "string" && d.entrypoint ? d.entrypoint : "main.py";
      const text = t("v2.coding.deploy.failure.entrypoint_missing", { entrypoint });
      const files = strings(d.candidates);
      return files.length ? `${text} ${t("v2.coding.deploy.failure.candidates", { files: files.join(", ") })}` : text;
    }
    if (d.reason === "dockerfile_missing") return t("v2.coding.deploy.failure.dockerfile_missing");
    if (d.reason === "invalid_spec") {
      const errors = strings(d.errors);
      if (errors.length) return t("v2.coding.deploy.failure.invalid_spec", { errors: errors.join("; ") });
    }
  }
  return errorMessage(err);
}

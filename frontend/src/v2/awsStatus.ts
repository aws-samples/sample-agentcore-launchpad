// N17-obs456 / obs457: an AgentCore / Bedrock-KB resource status (data source, ingestion job,
// KB document, memory, memory strategy, runtime version / endpoint, gateway / target), a
// platform job / run state or a trace span status, in the console language. The raw enum used to render
// as-is on the zh-CN console (「AVAILABLE」, 「COMPLETE」); the tone stays with the caller.
import type { TFunction } from "i18next";

/** Every status with a `v2.awsStatus.<CODE>` label (en + zh-CN). */
export const AWS_STATUS_CODES = [
  "AVAILABLE",
  "ACTIVE",
  "CREATING",
  "UPDATING",
  "DELETING",
  "DELETE_UNSUCCESSFUL",
  "DELETE_FAILED",
  "STARTING",
  "IN_PROGRESS",
  "COMPLETE",
  "COMPLETED",
  "FAILED",
  "STOPPING",
  "STOPPED",
  "PENDING",
  "INDEXING",
  "INDEXED",
  "PARTIALLY_INDEXED",
  "NOT_FOUND",
  // N17-obs457: AgentCore runtime / endpoint / gateway / target statuses
  "READY",
  "CREATE_FAILED",
  "UPDATE_FAILED",
  "CREATE_UNSUCCESSFUL",
  "UPDATE_UNSUCCESSFUL",
  "SYNCHRONIZING",
  "SYNCHRONIZE_UNSUCCESSFUL",
  // the platform's job / run states (lowercase on the wire; the lookup is case-insensitive)
  "SUCCEEDED",
  "QUEUED",
  "RUNNING",
  "CANCELLED",
  "CANCELED",
  "INTERRUPTED",
  "SKIPPED",
  "INVOKING",
  "WAITING",
  "EVALUATING",
  // trace span / tool-result statuses
  "OK",
  "ERROR",
  "UNSET",
  "SUCCESS",
] as const;

/** The status's display name (case-insensitive, like `resourceState`); an unknown status
 *  stays raw, a missing one reads "—". */
export function awsStatusLabel(t: TFunction, status: string | null | undefined): string {
  if (!status) return "—";
  const code = status.toUpperCase();
  return (AWS_STATUS_CODES as readonly string[]).includes(code) ? t(`v2.awsStatus.${code}`) : status;
}

/** N17-obs457: a deploy stage's detail that leads with a status code (the deploy stage's
 *  「READY · arn:…」) reads that code by its label; the rest of the text is the server's. */
export function wordStatusPrefix(t: TFunction, text: string | null | undefined): string {
  if (!text) return text ?? "";
  const m = /^([A-Z][A-Z_]+) · ([\s\S]*)$/.exec(text);
  if (!m || !(AWS_STATUS_CODES as readonly string[]).includes(m[1])) return text;
  return `${awsStatusLabel(t, m[1])} · ${m[2]}`;
}

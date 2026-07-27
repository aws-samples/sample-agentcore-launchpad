/** Display helpers for the Memory console.
 *
 * Deliberately local: `observability/format.ts` is trace/span-shaped (offsets,
 * durations, token costs) and shares nothing with memory events or records.
 */

/** ISO-8601 → locale date-time; nullish and unparsable values render as an em dash. */
export function stamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toLocaleString();
}

/** Blob payload size — memory blobs are agent state, typically well under 1 MB. */
export function bytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** Retrieval relevance — 3 decimals is where AgentCore scores stop being noise. */
export function score(value: number | null | undefined): string {
  return value == null ? "—" : value.toFixed(3);
}

/** Long ids (arns, 40-char session ids) shown inline; the full value goes in a title. */
export function shortId(value: string | null | undefined, keep = 12): string {
  if (!value) return "—";
  return value.length <= keep * 2 + 1 ? value : `${value.slice(0, keep)}…${value.slice(-keep)}`;
}

/** Status → Chip tone. Covers memory resource and strategy status. */
export function statusTone(
  status: string | null | undefined,
): "good" | "warn" | "crit" | "muted" {
  const s = (status ?? "").toUpperCase();
  if (s === "ACTIVE" || s === "SUCCEEDED" || s === "COMPLETED") return "good";
  if (s === "FAILED" || s === "ERROR") return "crit";
  if (!s) return "muted";
  return "warn"; // CREATING / PENDING / RUNNING / anything the preview API adds
}

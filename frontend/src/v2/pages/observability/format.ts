/** Number/time formatting shared by the Observability views. */
import { formatInZone, parseTimestamp } from "../../../lib/timestamps";

export function fmtCompact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}K`;
  return String(Math.round(n));
}

export function fmtInt(n: number): string {
  return Math.round(n).toLocaleString("en-US");
}

export function fmtDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function shortId(id: string | null | undefined, chars = 8): string {
  if (!id) return "—";
  return id.length > chars ? `${id.slice(0, chars)}…` : id;
}

export function fmtClock(iso: string | null | undefined): string {
  const d = parseTimestamp(iso); // N17-obs461
  if (!d) return "—";
  return formatInZone(d, "en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

export function fmtClockShort(iso: string | null | undefined): string {
  const d = parseTimestamp(iso);
  if (!d) return "—";
  return formatInZone(d, "en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** Logs Insights bin() buckets: UTC "YYYY-MM-DD HH:mm:ss.SSS" from AWS, sent by the API in the
 *  display zone ("…T18:00:00+08:00", N17-obs463) — both parse as the same instant. */
export function bucketDate(bucket: string): Date {
  return parseTimestamp(bucket) ?? new Date(Number.NaN); // an unreadable bucket stays invalid
}

export function fmtBucket(bucket: string, longRange: boolean): string {
  const d = bucketDate(bucket);
  if (Number.isNaN(d.getTime())) return bucket;
  const hm = formatInZone(d, "en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });
  if (!longRange) return hm;
  const md = formatInZone(d, "en-GB", { month: "2-digit", day: "2-digit" });
  return `${md} ${hm}`;
}

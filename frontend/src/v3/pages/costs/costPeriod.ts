/**
 * The cost view's time filter, like the AWS billing console: quick calendar periods in
 * UTC (the day AWS bills by; a week starts on Monday) and a custom date range. The
 * backend resolves every key (`services/costs.resolve_window`); this module only names
 * the choices and validates a custom range before it is sent.
 */

export const COST_PERIODS = [
  "today", "this_week", "7d", "last_week", "this_month", "30d", "last_month", "custom",
] as const;
export type CostPeriod = (typeof COST_PERIODS)[number];

export const DEFAULT_COST_PERIOD: CostPeriod = "7d";
/** the backend's `CUSTOM_MAX_DAYS` */
export const CUSTOM_MAX_DAYS = 92;

/** today's date in UTC, `YYYY-MM-DD` */
export function utcToday(now: Date = new Date()): string {
  return now.toISOString().slice(0, 10);
}

/** the i18n key suffix of what is wrong with a custom range, or null when it is fine */
export function customRangeError(start: string, end: string, now: Date = new Date()): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(start) || !/^\d{4}-\d{2}-\d{2}$/.test(end)) return "missing";
  const days = (Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86_400_000 + 1;
  if (days < 1) return "order";
  if (start > utcToday(now)) return "future";
  if (days > CUSTOM_MAX_DAYS) return "tooLong";
  return null;
}

/** "2026-10-26 – 2026-11-01" for a response's `period` (end exclusive at midnight → the day before) */
export function periodLabel(period: { start: string; end: string } | undefined): string {
  if (!period) return "";
  const start = period.start.slice(0, 10);
  const endAt = new Date(period.end);
  const midnight = endAt.getUTCHours() === 0 && endAt.getUTCMinutes() === 0 && endAt.getUTCSeconds() === 0;
  const last = midnight ? new Date(endAt.getTime() - 86_400_000) : endAt;
  const end = last.toISOString().slice(0, 10);
  return start === end ? start : `${start} – ${end}`;
}

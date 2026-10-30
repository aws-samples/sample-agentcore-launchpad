// Ledger timestamps arrive in two shapes: AWS-sourced ones carry an offset
// ("…+00:00"), but the SQLite ledger's own columns are written as naive UTC
// ("2026-10-07T06:20:04.314988"). `new Date()` reads a naive ISO string as LOCAL
// time, which shifted every ledger time by the browser's UTC offset (8 hours in
// UTC+8). Parse both as what they are. N17-obs461: every API time in the console
// parses through here (a source guard in v2/timezone.test.ts holds it), and a
// space-separated naive value ("2026-10-07 06:20:04") is UTC too.
const HAS_ZONE = /(Z|[+-]\d{2}:?\d{2})$/i;
const NAIVE_SPACE = /^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})/;

export function parseTimestamp(value: string | null | undefined): Date | null {
  if (!value) return null;
  const text = NAIVE_SPACE.test(value) ? value.replace(" ", "T") : value;
  const iso = HAS_ZONE.test(text) || !text.includes("T") ? text : `${text}Z`;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Epoch milliseconds of an API time, `NaN` when there is none or it does not parse. */
export function timeMs(value: string | null | undefined): number {
  return parseTimestamp(value)?.getTime() ?? Number.NaN;
}

/** Sort comparator, newest first, by instant (never by string: a naive and an offset-carrying
 *  value of the same moment compare unequal as text). A missing / unparsable value sorts last. */
export function compareTimesDesc(a: string | null | undefined, b: string | null | undefined): number {
  const x = timeMs(a);
  const y = timeMs(b);
  if (Number.isNaN(x) && Number.isNaN(y)) return 0;
  if (Number.isNaN(x)) return 1;
  if (Number.isNaN(y)) return -1;
  return y - x;
}

// N17-obs463 (user ruling 2026-10-26): the console shows every time in the DISPLAY zone the
// server names (GET /api/capabilities.display_timezone, default Asia/Shanghai) — never the
// browser's zone, so a UTC-zoned browser shows Beijing time too. Every formatter goes through
// `zonedParts` / `formatInZone` (a source guard in v2/timezone.test.ts holds it).
export const DEFAULT_DISPLAY_TIME_ZONE = "Asia/Shanghai";
let displayZone = DEFAULT_DISPLAY_TIME_ZONE;

/** Set from the capabilities payload; an unknown IANA name keeps the current zone. */
export function setDisplayTimeZone(zone: string | null | undefined): void {
  if (!zone) return;
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: zone });
    displayZone = zone;
  } catch {
    /* keep the current zone */
  }
}

export function displayTimeZone(): string {
  return displayZone;
}

export interface ZonedParts {
  year: number;
  month: number; // 1..12
  day: number;
  hour: number; // 0..23
  minute: number;
  second: number;
}

/** The wall-clock fields of an instant in the display zone. */
export function zonedParts(d: Date): ZonedParts {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: displayZone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).formatToParts(d);
  const get = (type: string) => Number(parts.find((p) => p.type === type)?.value ?? 0);
  return { year: get("year"), month: get("month"), day: get("day"), hour: get("hour"),
    minute: get("minute"), second: get("second") };
}

/** `Date.toLocaleString` in the display zone (date and/or time per `options`). */
export function formatInZone(d: Date, locale?: string, options: Intl.DateTimeFormatOptions = {}): string {
  return d.toLocaleString(locale, { ...options, timeZone: displayZone });
}

/** "18:29:54" — an API time's wall clock in the display zone; "" when it does not parse. */
export function zonedClock(value: string | null | undefined): string {
  const d = parseTimestamp(value);
  if (!d) return "";
  const { hour, minute, second } = zonedParts(d);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(hour)}:${pad(minute)}:${pad(second)}`;
}

/** An API time formatted in the display zone; `null` when it does not parse. */
export function formatApiTime(
  value: string | null | undefined, locale?: string, options: Intl.DateTimeFormatOptions = {},
): string | null {
  const d = parseTimestamp(value);
  return d ? formatInZone(d, locale, options) : null;
}

/** "2026-10-24" — an API time's calendar date in the display zone; "" when it does not parse. */
export function zonedDate(value: string | null | undefined): string {
  const d = parseTimestamp(value);
  if (!d) return "";
  const { year, month, day } = zonedParts(d);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${year}-${pad(month)}-${pad(day)}`;
}

const pad2 = (n: number) => String(n).padStart(2, "0");

/** "2026-10-24T18:29" — an epoch-millisecond instant as a `datetime-local` value in the
 *  display zone (the zone the operator reads every other time in). */
export function zonedInputValue(ms: number): string {
  const z = zonedParts(new Date(ms));
  return `${z.year}-${pad2(z.month)}-${pad2(z.day)}T${pad2(z.hour)}:${pad2(z.minute)}`;
}

/** The instant (epoch ms) a `datetime-local` value names when read as a display-zone wall
 *  clock; `null` when it does not parse. Two passes settle a DST edge. */
export function zonedInputToMs(value: string): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value.trim());
  if (!m) return null;
  const [, y, mo, d, h, mi, s] = m.map(Number);
  const wall = Date.UTC(y, mo - 1, d, h, mi, s || 0);
  let guess = wall;
  for (let i = 0; i < 2; i += 1) {
    const z = zonedParts(new Date(guess));
    const shown = Date.UTC(z.year, z.month - 1, z.day, z.hour, z.minute, z.second);
    guess += wall - shown;
  }
  return guess;
}

/** An epoch-millisecond instant as the ISO string a request body carries. */
export function isoFromMs(ms: number): string {
  return new Date(ms).toISOString();
}

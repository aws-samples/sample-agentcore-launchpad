// Ledger timestamps arrive in two shapes: AWS-sourced ones carry an offset
// ("…+00:00"), but the SQLite ledger's own columns are written as naive UTC
// ("2026-10-07T06:20:04.314988"). `new Date()` reads a naive ISO string as LOCAL
// time, which shifted every ledger time by the browser's UTC offset (8 hours in
// UTC+8). Parse both as what they are.
const HAS_ZONE = /(Z|[+-]\d{2}:?\d{2})$/i;

export function parseTimestamp(value: string | null | undefined): Date | null {
  if (!value) return null;
  const iso = HAS_ZONE.test(value) || !value.includes("T") ? value : `${value}Z`;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

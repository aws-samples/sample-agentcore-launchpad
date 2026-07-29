// Shared display formatting for evaluation/experiment numbers. Score means come
// off AWS as raw aggregates (0.5566666666666668) — every surface renders them at
// 2dp so the console reads consistently, and a missing value reads as an em dash
// rather than "null".
export function fmtScore(value: number | null | undefined): string {
  return value == null ? "—" : value.toFixed(2);
}

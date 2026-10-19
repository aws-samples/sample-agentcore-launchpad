// Display formatting for the coding IDE (pure).
import type { TokenUsage } from "./codingEvents";

/** Elapsed time as `m:ss`, or `h:mm:ss` from an hour (turns may run for hours). */
export function fmtClock(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return "—";
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/** 950 → "950", 1_200 → "1.2k", 38_512 → "39k", 2_400_000 → "2.4M". */
export function fmtTokens(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) return `${Number((n / 1000).toFixed(n < 10_000 ? 1 : 0))}k`;
  return `${Number((n / 1_000_000).toFixed(1))}M`;
}

export interface UsageLabels {
  input: string;
  cached: string;
  cacheWrite: string;
  output: string;
  reasoning: string;
}

/** The breakdown shown as a tooltip on a turn's token count. */
export function usageBreakdown(usage: TokenUsage, labels: UsageLabels): string {
  const n = (v: number) => v.toLocaleString("en-US");
  return [
    `${labels.input} ${n(usage.input)}`,
    `${labels.cached} ${n(usage.cachedInput)}`,
    usage.cacheWrite ? `${labels.cacheWrite} ${n(usage.cacheWrite)}` : null,
    `${labels.output} ${n(usage.output)}`,
    usage.reasoningOutput ? `${labels.reasoning} ${n(usage.reasoningOutput)}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

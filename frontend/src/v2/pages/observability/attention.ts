// What on the observability dashboard needs a look, ranked. Pure: the page turns
// each finding into a row; the thresholds are the ones the console states.
import type { ObsDashboard } from "../../../lib/api";

/** Above this share of failed traces the error rate reads as a problem. */
export const ERROR_RATE_ACT = 0.05;
/** A bucket whose error share is this much above the range average is a spike. */
const SPIKE_FACTOR = 3;
/** A spike needs at least this many errors in its bucket to be worth a row. */
const SPIKE_MIN_ERRORS = 2;
/** P95 latency above this is slow (amber); above the timeout it is failing (coral). */
export const P95_SLOW_MS = 10_000;
export const P95_TIMEOUT_MS = 30_000;
/** A tool under this success rate, with at least one error, is failing. */
const TOOL_OK_PCT = 90;

export type FindingKind = "errorRate" | "errorSpike" | "slowP95" | "toolFailing";

export interface Finding {
  kind: FindingKind;
  s: "act" | "wait";
  /** values the copy interpolates */
  values: Record<string, string | number>;
  /** where the finding is handled: the traces tab, optionally filtered */
  target: { tab: "traces"; status?: "error" } | null;
}

export function findings(data: ObsDashboard): Finding[] {
  const out: Finding[] = [];
  const { tiles, series, top_tools } = data;
  if (tiles.traces.total > 0 && tiles.error_rate > ERROR_RATE_ACT) {
    out.push({
      kind: "errorRate",
      s: "act",
      values: { rate: (tiles.error_rate * 100).toFixed(1), errors: tiles.traces.error, total: tiles.traces.total },
      target: { tab: "traces", status: "error" },
    });
  }
  const avg = tiles.traces.total > 0 ? tiles.traces.error / tiles.traces.total : 0;
  let spike: ObsDashboard["series"][number] | null = null;
  for (const b of series) {
    if (b.traces === 0 || b.errors < SPIKE_MIN_ERRORS) continue;
    const share = b.errors / b.traces;
    if (share > Math.max(avg * SPIKE_FACTOR, 0.1) && (!spike || b.errors > spike.errors)) spike = b;
  }
  if (spike) {
    out.push({
      kind: "errorSpike",
      s: "wait",
      values: { bucket: spike.bucket, errors: spike.errors, traces: spike.traces },
      target: { tab: "traces", status: "error" },
    });
  }
  if (tiles.latency.p95_ms > P95_SLOW_MS) {
    out.push({
      kind: "slowP95",
      s: tiles.latency.p95_ms > P95_TIMEOUT_MS ? "act" : "wait",
      values: { p95: tiles.latency.p95_ms },
      target: { tab: "traces" },
    });
  }
  for (const tool of top_tools) {
    if (tool.errors > 0 && tool.success_rate != null && tool.success_rate < TOOL_OK_PCT) {
      out.push({
        kind: "toolFailing",
        s: tool.success_rate < 50 ? "act" : "wait",
        values: { tool: tool.tool, errors: tool.errors, calls: tool.calls, rate: tool.success_rate },
        target: { tab: "traces", status: "error" },
      });
    }
  }
  // coral first, then the order above (rate, spike, latency, tools)
  return out.sort((a, b) => (a.s === b.s ? 0 : a.s === "act" ? -1 : 1));
}

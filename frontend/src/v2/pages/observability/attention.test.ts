import { describe, expect, it } from "vitest";

import type { ObsDashboard } from "../../../lib/api";
import { findings, P95_TIMEOUT_MS } from "./attention";
import { TIMEOUT_MS } from "./common";

function dash(over: Partial<ObsDashboard["tiles"]> = {}, rest: Partial<ObsDashboard> = {}): ObsDashboard {
  return {
    range: "24h",
    tiles: {
      traces: { total: 100, ok: 100, error: 0 },
      sessions: { total: 10, agents: 2 },
      error_rate: 0,
      latency: { p50_ms: 800, p95_ms: 2000 },
      tokens: { input: 1, output: 1, total: 2, est_cost_usd: 0 },
      ...over,
    },
    series: [],
    tokens_by_model: [],
    top_tools: [],
    cache: { hit: false, age_seconds: 0 },
    ...rest,
  };
}

describe("observability findings", () => {
  it("is quiet when nothing is off", () => {
    expect(findings(dash())).toEqual([]);
  });

  it("flags an error rate above 5% and sends it to the failed traces", () => {
    const f = findings(dash({ traces: { total: 100, ok: 90, error: 10 }, error_rate: 0.1 }));
    expect(f[0]).toMatchObject({ kind: "errorRate", s: "act", target: { tab: "traces", status: "error" } });
  });

  it("finds the worst error spike in the series", () => {
    const f = findings(
      dash({ traces: { total: 100, ok: 96, error: 4 }, error_rate: 0.04 }, {
        series: [
          { bucket: "a", traces: 50, errors: 0, p50_ms: 1, p95_ms: 1 },
          { bucket: "b", traces: 10, errors: 4, p50_ms: 1, p95_ms: 1 },
        ],
      }),
    );
    expect(f.map((x) => x.kind)).toEqual(["errorSpike"]);
    expect(f[0].values.bucket).toBe("b");
  });

  it("grades slow P95 by the timeout", () => {
    expect(findings(dash({ latency: { p50_ms: 1, p95_ms: 12_000 } }))[0]).toMatchObject({ kind: "slowP95", s: "wait" });
    expect(findings(dash({ latency: { p50_ms: 1, p95_ms: 45_000 } }))[0]).toMatchObject({ kind: "slowP95", s: "act" });
  });

  it("lists failing tools, coral first", () => {
    const f = findings(
      dash({}, {
        top_tools: [
          { tool: "ok", calls: 10, errors: 0, success_rate: 100 },
          { tool: "flaky", calls: 10, errors: 2, success_rate: 80 },
          { tool: "broken", calls: 10, errors: 8, success_rate: 20 },
        ],
      }),
    );
    expect(f.map((x) => x.values.tool)).toEqual(["broken", "flaky"]);
  });

  it("stays quiet exactly at each threshold", () => {
    expect(findings(dash({ traces: { total: 100, ok: 95, error: 5 }, error_rate: 0.05 }))).toEqual([]);
    expect(findings(dash({ latency: { p50_ms: 1, p95_ms: 10_000 } }))).toEqual([]);
    expect(findings(dash({ latency: { p50_ms: 1, p95_ms: 30_000 } }))[0]).toMatchObject({ s: "wait" });
    expect(findings(dash({}, { top_tools: [{ tool: "t", calls: 10, errors: 1, success_rate: 90 }] }))).toEqual([]);
    expect(findings(dash({}, { top_tools: [{ tool: "t", calls: 10, errors: 5, success_rate: 50 }] }))[0]).toMatchObject({ s: "wait" });
    // a spike needs at least two errors in its bucket
    expect(findings(dash({}, { series: [{ bucket: "a", traces: 2, errors: 1, p50_ms: 1, p95_ms: 1 }] }))).toEqual([]);
  });

  it("uses the same timeout the trace list marks", () => {
    expect(P95_TIMEOUT_MS).toBe(TIMEOUT_MS);
  });
});

import { describe, expect, it } from "vitest";

import type { ChatTraceInfo } from "../../../lib/api";
import { inspectTab, SPAN_LIMIT, spanBars } from "./inspect";

const trace = (spans: { start_ms?: number; duration_ms?: number }[]): ChatTraceInfo => ({
  span_count: spans.length,
  cloudwatch_url: "",
  spans: spans.map((s, i) => ({ name: `s${i}`, category: "model", ...s })) as ChatTraceInfo["spans"],
});

describe("inspectTab", () => {
  it("accepts the two tabs and closes on anything else", () => {
    expect(inspectTab("trace")).toBe("trace");
    expect(inspectTab("memory")).toBe("memory");
    expect(inspectTab("api")).toBeNull();
    expect(inspectTab(null)).toBeNull();
  });
});

describe("spanBars", () => {
  it("places spans on the drawn extent", () => {
    const bars = spanBars(trace([{ start_ms: 0, duration_ms: 100 }, { start_ms: 50, duration_ms: 50 }]));
    expect(bars.map((b) => [b.left, b.width])).toEqual([[0, 100], [50, 50]]);
  });

  it("keeps an instant span visible and survives missing timings", () => {
    const bars = spanBars(trace([{ start_ms: 0, duration_ms: 1000 }, { start_ms: 1000 }, {}]));
    expect(bars[1].width).toBe(0.8);
    expect(bars[2]).toMatchObject({ left: 0, width: 0.8 });
  });

  it("draws at most the limit and nothing without a trace", () => {
    expect(spanBars(trace(Array.from({ length: 20 }, () => ({ duration_ms: 1 }))))).toHaveLength(SPAN_LIMIT);
    expect(spanBars(null)).toEqual([]);
  });
});

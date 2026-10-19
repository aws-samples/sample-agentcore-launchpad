import { describe, expect, it } from "vitest";

import { fmtClock, fmtTokens, usageBreakdown } from "./format";

describe("coding format", () => {
  it("formats elapsed time", () => {
    expect(fmtClock(null)).toBe("—");
    expect(fmtClock(-1)).toBe("—");
    expect(fmtClock(0)).toBe("0:00");
    expect(fmtClock(65_400)).toBe("1:05");
    expect(fmtClock(3_600_000 + 61_000)).toBe("1:01:01");
    expect(fmtClock(16 * 60_000)).toBe("16:00");
  });

  it("formats token counts", () => {
    expect(fmtTokens(undefined)).toBe("—");
    expect(fmtTokens(950)).toBe("950");
    expect(fmtTokens(1200)).toBe("1.2k");
    expect(fmtTokens(38_512)).toBe("39k");
    expect(fmtTokens(2_400_000)).toBe("2.4M");
  });

  const labels = { input: "input", cached: "cached", cacheWrite: "cache write", output: "output", reasoning: "reasoning" };

  it("lists the usage breakdown, skipping empty cache writes and reasoning", () => {
    expect(usageBreakdown({ input: 38_500, cachedInput: 25_500, cacheWrite: 0, output: 900, reasoningOutput: 0, total: 39_400 }, labels)).toBe(
      "input 38,500 · cached 25,500 · output 900",
    );
    expect(usageBreakdown({ input: 1, cachedInput: 0, cacheWrite: 2, output: 3, reasoningOutput: 4, total: 4 }, labels)).toBe(
      "input 1 · cached 0 · cache write 2 · output 3 · reasoning 4",
    );
  });
});

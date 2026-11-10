import { describe, expect, it } from "vitest";

import { boundedInterval, combined, significance, studentTTwoSidedP, welchP, wilson } from "./significance";

// pins computed by the backend (`optimization/stats`, `evaluation/run_stats`) — the two
// implementations must agree
describe("significance (EVAL-TRUST 3.2)", () => {
  it("matches the backend t distribution", () => {
    expect(studentTTwoSidedP(2.1, 7.5)).toBeCloseTo(0.071237, 5);
    expect(studentTTwoSidedP(0.4, 3)).toBeCloseTo(0.715935, 5);
    expect(studentTTwoSidedP(5, 30)).toBeCloseTo(2.32967e-5, 8);
  });

  const low = { sessions_n: 4, session_mean: 0.1425, sd: 0.043493, ci95: [0.0733, 0.2117] as [number, number] };
  const high = { sessions_n: 4, session_mean: 0.895, sd: 0.042032, ci95: [0.8281, 0.9619] as [number, number] };
  const noisy = { sessions_n: 4, session_mean: 0.525, sd: 0.457347, ci95: [-0.2027, 1.2527] as [number, number] };

  it("matches the backend Welch p from summaries", () => {
    expect(welchP(low, high)).toBeCloseTo(2.81008e-7, 9);
    expect(welchP(low, noisy)).toBeCloseTo(0.192849, 4);
  });

  it("labels a difference", () => {
    expect(significance(low, high)).toBe("significant");
    expect(significance(low, noisy)).toBe("not_significant");
    expect(significance(low, { sessions_n: 1, session_mean: 0.9, sd: null, ci95: null })).toBe("insufficient");
    expect(significance(low, undefined)).toBe("insufficient");
  });

  it("combines evaluators", () => {
    expect(combined(["not_significant", "significant"])).toBe("significant");
    expect(combined(["not_significant", "insufficient"])).toBe("not_significant");
    expect(combined(["insufficient"])).toBe("insufficient");
  });

  // N17-obs423 pins (backend `run_stats.wilson`, `summarize` clamp)
  it("matches the backend Wilson interval", () => {
    const [lo, hi] = wilson(5, 7);
    expect(lo).toBeCloseTo(0.358934, 5);
    expect(hi).toBeCloseTo(0.917781, 5);
    expect(wilson(6, 6)[0]).toBeCloseTo(0.609666, 5);
    expect(wilson(6, 6)[1]).toBe(1);
  });

  it("clamps an interval to its scale", () => {
    // run 2d3fa04f0471, stored before the clamp
    expect(boundedInterval({ evaluatorId: "Builtin.Correctness", sessions_n: 7, session_mean: 0.714, sd: 0.4, ci95: [0.263, 1.166] })).toEqual([0.263, 1]);
    expect(boundedInterval({ sessions_n: 4, session_mean: 4.75, sd: 0.5, ci95: [3.95, 5.55], scale: [1, 5] })).toEqual([3.95, 5]);
    expect(boundedInterval({ evaluatorId: "cust_x", sessions_n: 4, session_mean: 4.75, sd: 0.5, ci95: [3.95, 5.55] })).toEqual([3.95, 5.55]);
  });
});

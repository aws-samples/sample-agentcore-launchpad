import { describe, expect, it } from "vitest";

import { normalizedInterval, normalizedScore } from "./format";

// N17-obs430: the 按评估器 card showed a penalty evaluator's mean normalized (拒答 0.2308 → 0.77)
// next to its RAW interval (0.07–0.48); both must sit on the same oriented scale.
describe("normalizedInterval (N17-obs430)", () => {
  const refusal = { evaluatorId: "Builtin.Refusal", sessions_n: 20, session_mean: 0.2308, sd: 0.4, ci95: [0.0674, 0.4826] as [number, number] };

  it("mirrors a penalty evaluator's interval like its mean", () => {
    expect(normalizedScore(0.2308, "Builtin.Refusal")).toBeCloseTo(0.7692, 6);
    const ci = normalizedInterval(refusal);
    expect(ci).not.toBeNull();
    const [lo, hi] = ci as [number, number];
    expect(lo).toBeCloseTo(0.5174, 6);
    expect(hi).toBeCloseTo(0.9326, 6);
    expect(lo).toBeLessThanOrEqual(hi);
    // the width is a property of the sample, not of the orientation
    expect(hi - lo).toBeCloseTo(0.4826 - 0.0674, 9);
    expect(normalizedScore(0.2308, "Builtin.Refusal")).toBeGreaterThan(lo);
    expect(normalizedScore(0.2308, "Builtin.Refusal")).toBeLessThan(hi);
  });

  it("mirrors the DeepEval penalty metrics too, on [0, 1] when no scale is stored", () => {
    expect(normalizedInterval({ evaluatorId: "ThirdParty.DeepEval.Toxicity", ci95: [0.1, 0.3] })).toEqual([0.7, 0.9]);
    expect(normalizedInterval({ evaluatorId: "ThirdParty.DeepEval.Bias", ci95: [0.0, 0.25] })).toEqual([0.75, 1]);
  });

  it("leaves a positive-polarity interval unchanged (after the N17-obs423 clamp)", () => {
    expect(normalizedInterval({ evaluatorId: "Builtin.Helpfulness", sessions_n: 20, ci95: [0.61, 0.83] })).toEqual([0.61, 0.83]);
    expect(normalizedInterval({ evaluatorId: "Builtin.Correctness", sessions_n: 7, ci95: [0.263, 1.166] })).toEqual([0.263, 1]);
    expect(normalizedInterval({ evaluatorId: "cust_f623957dd060", ci95: [0.2, 0.4] })).toEqual([0.2, 0.4]);
  });

  it("clamps first, then mirrors, so the result stays inside the scale", () => {
    // a small-n Refusal t-interval leaking below 0 (stored before the clamp)
    expect(normalizedInterval({ evaluatorId: "Builtin.Refusal", sessions_n: 4, ci95: [-0.2027, 0.5] })).toEqual([0.5, 1]);
    // a stored scale mirrors about its end points
    const ci = normalizedInterval({ evaluatorId: "Builtin.Refusal", ci95: [1.5, 2.5], scale: [1, 5] }) as [number, number];
    expect(ci[0]).toBeCloseTo(3.5, 9);
    expect(ci[1]).toBeCloseTo(4.5, 9);
  });

  it("has no interval when the entry has none", () => {
    expect(normalizedInterval({ evaluatorId: "Builtin.Refusal", ci95: null })).toBeNull();
    expect(normalizedInterval({ evaluatorId: "Builtin.Refusal" })).toBeNull();
  });
});

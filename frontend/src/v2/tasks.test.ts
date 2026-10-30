import { describe, expect, it } from "vitest";

import { needsAbsentGroundTruth } from "./tasks";

describe("needsAbsentGroundTruth (N17-obs437)", () => {
  const gt = { requires_ground_truth: true };
  const plain = { requires_ground_truth: false };

  it("blocks a ground-truth evaluator where no reference exists", () => {
    expect(needsAbsentGroundTruth(gt, "continuous", "dataset")).toBe(true);
    for (const source of ["sessions", "window", "logs"]) {
      expect(needsAbsentGroundTruth(gt, "history", source)).toBe(true);
    }
  });

  it("allows it on a dataset run, and never blocks one that reads no ground truth", () => {
    expect(needsAbsentGroundTruth(gt, "history", "dataset")).toBe(false);
    expect(needsAbsentGroundTruth(plain, "continuous", "sessions")).toBe(false);
    expect(needsAbsentGroundTruth({}, "continuous", "sessions")).toBe(false);
  });
});

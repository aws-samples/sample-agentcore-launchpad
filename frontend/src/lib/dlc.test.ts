import { describe, expect, it } from "vitest";

import { type CostEstimate, canRunGate, compoundRate, gateTone, goldenVersionsLabel, kappaTone, tierTone, unpricedJudges } from "./dlc";

describe("canRunGate", () => {
  it("offers the gate for a never-evaluated candidate and again after an INVALID verdict", () => {
    expect(canRunGate(null)).toBe(false);
    expect(canRunGate({ decision: "pending", run_ids: [] })).toBe(true);
    // a gate run interrupted by a backend restart leaves the evidence incomplete
    expect(canRunGate({ decision: "invalid", run_ids: ["r1", "r2"] })).toBe(true);
  });

  it("does not re-run an evaluating, blocked or released candidate from the run panel", () => {
    expect(canRunGate({ decision: "evaluating", run_ids: ["r1"] })).toBe(false);
    expect(canRunGate({ decision: "blocked", run_ids: ["r1", "r2"] })).toBe(false);
    expect(canRunGate({ decision: "released", run_ids: ["r1", "r2"] })).toBe(false);
  });
});

describe("compoundRate", () => {
  it("multiplies the gates, which is the number nobody types on purpose", () => {
    // the design's worked example: eight 95% gates let 66% of sessions through
    const eight = compoundRate(Array.from({ length: 8 }, () => 0.95));
    expect(eight).not.toBeNull();
    expect(eight as number).toBeCloseTo(0.6634, 4);
  });

  it("is the single threshold when there is only one gate", () => {
    expect(compoundRate([0.9])).toBeCloseTo(0.9, 10);
  });

  it("says nothing rather than 1.0 when no gate has a threshold", () => {
    // a metric gate carries a bound, not a rate: counting it as 1.0 would make a
    // table of metric gates look like a certainty
    expect(compoundRate([])).toBeNull();
    expect(compoundRate([0, 1.5, Number.NaN])).toBeNull();
  });

  it("ignores unusable thresholds instead of poisoning the product", () => {
    expect(compoundRate([0.9, 0, 2])).toBeCloseTo(0.9, 10);
  });
});

describe("kappaTone", () => {
  it("follows the Landis–Koch bands", () => {
    expect(kappaTone(0.85)).toBe("green");
    expect(kappaTone(0.8)).toBe("green");
    expect(kappaTone(0.61)).toBe("blue");
    expect(kappaTone(0.45)).toBe("orange");
    expect(kappaTone(0.1)).toBe("red");
    expect(kappaTone(-0.2)).toBe("red");
  });

  it("is grey when there is no measurement, not green", () => {
    expect(kappaTone(null)).toBe("gray");
  });
});

describe("gateTone", () => {
  it("keeps 'cannot decide' visually apart from 'failed'", () => {
    expect(gateTone("BLOCKED")).toBe("red");
    expect(gateTone("INVALID")).toBe("orange");
    expect(gateTone("PASS")).toBe("green");
    expect(gateTone("WAIVED")).toBe("blue");
    expect(gateTone("OBSERVED")).toBe("gray");
    expect(gateTone(null)).toBe("gray");
  });
});

describe("tierTone", () => {
  it("colours a red line as a red line", () => {
    expect(tierTone("redline")).toBe("red");
    expect(tierTone("gate")).toBe("orange");
    expect(tierTone("observe")).toBe("gray");
  });
});

describe("unpricedJudges", () => {
  const base = { judges: [] } as unknown as CostEstimate;
  it("names the self-billed judges the price map cannot value", () => {
    const est = { ...base, judges: [
      { evaluator_id: "Builtin.Correctness", billed_by: "agentcore_evaluations", usd: null },
      { evaluator_id: "fee-check", billed_by: "your_account", usd: null, note: "judge model is not in the price map" },
      { evaluator_id: "privacy", billed_by: "your_account", usd: 0.12 },
    ] } as CostEstimate;
    // the service-billed builtin is not "unpriced": it is not this account's bill
    expect(unpricedJudges(est).map((j) => j.evaluator_id)).toEqual(["fee-check"]);
  });
  it("is empty without an estimate", () => {
    expect(unpricedJudges(null)).toEqual([]);
    expect(unpricedJudges(base)).toEqual([]);
  });
});

describe("goldenVersionsLabel", () => {
  it("names each frozen split instead of printing [object Object]", () => {
    // what releases.py stores today: dataset id, version label (null when the
    // split is unversioned) and the active item count
    const label = goldenVersionsLabel({
      regression: { dataset_id: "4327ce7e1e2b", version: null, items: 15 },
      holdout: { dataset_id: "7746f6ab7ec7", version: "v2", items: 9 },
    });
    expect(label).toBe("regression 4327ce7e1e2b (15) · holdout v2 (9)");
    expect(label).not.toContain("[object Object]");
  });

  it("still reads older records that stored a bare label, and empty ones", () => {
    expect(goldenVersionsLabel({ regression: "v1", holdout: null })).toBe("regression v1 · holdout —");
    expect(goldenVersionsLabel({})).toBe("—");
    expect(goldenVersionsLabel(undefined)).toBe("—");
  });
});

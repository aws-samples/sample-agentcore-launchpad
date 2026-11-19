import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { evaluatorScoreLabel, FAILURE_ANALYSIS_EVALUATOR } from "../lib/evaluators";
import { fmtTime, normalizedScore } from "./format";

describe("fmtTime", () => {
  // render in UTC+8 whatever the host zone: in UTC a naive string reads the same
  // either way, so the shift would pass unseen
  const hostTz = process.env.TZ;
  beforeAll(() => {
    process.env.TZ = "Asia/Shanghai";
  });
  afterAll(() => {
    if (hostTz === undefined) delete process.env.TZ;
    else process.env.TZ = hostTz;
  });

  it("renders a naive ledger timestamp as UTC in the local zone", () => {
    expect(fmtTime("2026-10-08T10:01:13.754719")).toBe("2026-10-08 18:01:13");
  });

  it("keeps an explicit offset", () => {
    expect(fmtTime("2026-10-08T10:01:13+00:00")).toBe("2026-10-08 18:01:13");
  });

  it("falls back for missing or unparsable values", () => {
    expect(fmtTime(null)).toBe("—");
    expect(fmtTime("not a date")).toBe("not a date");
  });
});

describe("Failure Analysis score", () => {
  it("shows the raw failure percentage while orienting quality scores higher-is-better", () => {
    expect(evaluatorScoreLabel(0.2, FAILURE_ANALYSIS_EVALUATOR)).toBe("20%");
    expect(normalizedScore(0.2, FAILURE_ANALYSIS_EVALUATOR)).toBe(0.8);
    expect(evaluatorScoreLabel(0.2, "Builtin.GoalSuccessRate")).toBe("0.20");
    expect(normalizedScore(0.2, "Builtin.GoalSuccessRate")).toBe(0.2);
  });
});

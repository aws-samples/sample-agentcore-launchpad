import { describe, expect, it } from "vitest";

import type { EvaluationRunInfo } from "../lib/evaluation";
import { isOnlineReport } from "./tasks";

const run = (dataset_name: string | null) => ({ dataset_name }) as EvaluationRunInfo;

describe("isOnlineReport", () => {
  it("is an online config's insights report, which 一次性评估 leaves to the config's 报告 panel", () => {
    expect(isOnlineReport(run("online:oe_hr_assistant_rt_73532d-8c6mYqCIRH"))).toBe(true);
    expect(isOnlineReport(run("window:24"))).toBe(false);
    expect(isOnlineReport(run("e2e-math-mini"))).toBe(false);
    expect(isOnlineReport(run(null))).toBe(false);
  });
});

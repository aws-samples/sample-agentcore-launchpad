import { describe, expect, it } from "vitest";

import { defaultPrimary, evaluatorIdOf } from "../v2/pages/canary/evaluatorChoice";
import type { EvaluatorRow } from "./api";

const rows: EvaluatorRow[] = [
  { id: "Builtin.GoalSuccessRate", level: "SESSION", source: "builtin" },
  { id: "Builtin.Helpfulness", level: "TRACE", source: "builtin" },
  { id: "ThirdParty.DeepEval.Bias", level: "TRACE", source: "third_party" },
  { id: "StoreRules-abc123", name: "store_rules", level: "SESSION", source: "custom" },
];

describe("canary default primary evaluator", () => {
  it("prefers the first selected custom judge", () => {
    expect(defaultPrimary(["Builtin.Helpfulness", "StoreRules-abc123", "Builtin.GoalSuccessRate"], rows)).toBe(
      "StoreRules-abc123",
    );
  });

  it("falls back to goal success rate, then the first pick", () => {
    expect(defaultPrimary(["Builtin.Helpfulness", "Builtin.GoalSuccessRate"], rows)).toBe("Builtin.GoalSuccessRate");
    expect(defaultPrimary(["ThirdParty.DeepEval.Bias", "Builtin.Helpfulness"], rows)).toBe("ThirdParty.DeepEval.Bias");
    expect(defaultPrimary([], rows)).toBe("");
  });

  it("treats an id the listing has not loaded yet as custom unless it is managed", () => {
    expect(defaultPrimary(["Builtin.Helpfulness", "Judge-xyz"], [])).toBe("Judge-xyz");
    expect(defaultPrimary(["ThirdParty.DeepEval.Bias", "Builtin.GoalSuccessRate"], [])).toBe("Builtin.GoalSuccessRate");
  });

  it("reads the evaluator id off an ARN", () => {
    expect(evaluatorIdOf("arn:aws:bedrock-agentcore:us-west-2:111122223333:evaluator/StoreRules-abc123")).toBe(
      "StoreRules-abc123",
    );
    expect(evaluatorIdOf("Builtin.Helpfulness")).toBe("Builtin.Helpfulness");
  });
});

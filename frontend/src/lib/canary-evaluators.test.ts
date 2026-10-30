import { describe, expect, it } from "vitest";

import { defaultPrimary, evaluatorIdOf } from "../v2/pages/canary/evaluatorChoice";
import type { EvaluatorRow } from "./api";

/** N12-obs46 (upstream b5eb3dc8 parity): the canary start pages propose a primary
 *  evaluator and read metric ids off the stored evaluator id / ARN. */

const rows: EvaluatorRow[] = [
  { id: "Builtin.GoalSuccessRate", level: "SESSION", source: "builtin" },
  { id: "Builtin.Helpfulness", level: "TRACE", source: "builtin" },
  { id: "ThirdParty.DeepEval.Bias", level: "TRACE", source: "third_party" },
  { id: "cust_abc123", name: "store_rules", level: "SESSION", source: "custom" },
];

describe("canary default primary evaluator", () => {
  it("prefers the first selected custom judge", () => {
    expect(defaultPrimary(["Builtin.Helpfulness", "cust_abc123", "Builtin.GoalSuccessRate"], rows)).toBe("cust_abc123");
  });

  it("falls back to goal success rate, then the first pick", () => {
    expect(defaultPrimary(["Builtin.Helpfulness", "Builtin.GoalSuccessRate"], rows)).toBe("Builtin.GoalSuccessRate");
    expect(defaultPrimary(["ThirdParty.DeepEval.Bias", "Builtin.Helpfulness"], rows)).toBe("ThirdParty.DeepEval.Bias");
    expect(defaultPrimary([], rows)).toBe("");
  });

  it("treats an id the listing has not loaded yet as custom unless it is managed", () => {
    expect(defaultPrimary(["Builtin.Helpfulness", "cust_xyz"], [])).toBe("cust_xyz");
    expect(defaultPrimary(["ThirdParty.DeepEval.Bias", "Builtin.GoalSuccessRate"], [])).toBe("Builtin.GoalSuccessRate");
  });

  it("reads the evaluator id off an ARN or a bare id", () => {
    expect(evaluatorIdOf("arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:evaluator/cust_abc123")).toBe("cust_abc123");
    expect(evaluatorIdOf("Builtin.Helpfulness")).toBe("Builtin.Helpfulness");
  });
});

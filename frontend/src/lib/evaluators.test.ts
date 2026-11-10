import type { TFunction } from "i18next";
import { describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { scoreTone } from "../v2/format";
import { setCustomEvaluatorNames } from "./evaluator-names";
import { EVALUATOR_NAME_RE, evaluatorLabel, LEVEL_PLACEHOLDERS, passThreshold, scoreColor } from "./evaluators";

describe("evaluator form placeholders (N17-obs311)", () => {
  it("treats the actual tool trajectory as trace-derived, not ground truth", () => {
    expect(LEVEL_PLACEHOLDERS.SESSION.core).toContain("{actual_tool_trajectory}");
    expect(LEVEL_PLACEHOLDERS.SESSION.groundTruth).not.toContain("{actual_tool_trajectory}");
  });

  it("tags exactly the backend's ground-truth placeholders", () => {
    // backend strands_eval/judge_placeholders.GROUND_TRUTH_PLACEHOLDERS
    const backend = ["{assertions}", "{expected_response}", "{expected_tool_trajectory}"];
    const tagged = new Set(Object.values(LEVEL_PLACEHOLDERS).flatMap((p) => p.groundTruth));
    expect([...tagged].sort()).toEqual(backend);
  });
});

describe("evaluator name rule (N17-obs312)", () => {
  it("is the backend JudgeCreate.name pattern", () => {
    expect(EVALUATOR_NAME_RE.source).toBe("^[a-zA-Z][a-zA-Z0-9_]{0,47}$");
    expect(EVALUATOR_NAME_RE.test("a".repeat(48))).toBe(true);
    expect(EVALUATOR_NAME_RE.test("a".repeat(49))).toBe(false);
    expect(EVALUATOR_NAME_RE.test("hr-judge")).toBe(false);
    expect(EVALUATOR_NAME_RE.test("1judge")).toBe(false);
  });

  it("is what the name hint says in both languages", () => {
    const hints = [en.v2.evaluators.nameHint, zh.v2.evaluators.nameHint];
    for (const hint of hints) {
      expect(hint).toContain("48");
      expect(hint).not.toMatch(/100|hyphen|连字符/);
    }
    expect(en.v2.evaluators.nameHint).toMatch(/start with a letter/i);
    expect(zh.v2.evaluators.nameHint).toContain("以字母开头");
  });
});

describe("per-evaluator pass bar (N17-obs425)", () => {
  it("colours a Mostly Followed row as not passed, a Helpfulness 0.833 row as passed", () => {
    expect(passThreshold("Builtin.SkillInstructionFollowing")).toBe(1);
    expect(passThreshold("arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:evaluator/Builtin.GoalSuccessRate")).toBe(1);
    expect(passThreshold("my-judge")).toBe(0.7);
    expect(scoreColor(0.75, "Builtin.SkillInstructionFollowing")).not.toBe("var(--good)");
    expect(scoreColor(1, "Builtin.SkillInstructionFollowing")).toBe("var(--good)");
    expect(scoreColor(0.833, "Builtin.Helpfulness")).toBe("var(--good)");
    expect(scoreTone(0.75, "Builtin.SkillInstructionFollowing")).toBe("mid");
    expect(scoreTone(0.75)).toBe("good"); // a mean keeps the default bar
  });
});

// N17-obs429: custom evaluators by their catalogue name; a deleted one keeps its id
describe("evaluatorLabel with the evaluator catalogue", () => {
  it("names a custom evaluator and falls back to the id without a row", () => {
    const t = ((key: string, opts?: { defaultValue?: string }) => opts?.defaultValue ?? key) as unknown as TFunction;
    setCustomEvaluatorNames({ cust_f623957dd060: "hr_policy_citation" });
    expect(evaluatorLabel(t, "cust_f623957dd060")).toBe("hr_policy_citation");
    expect(evaluatorLabel(t, "cust_deadbeef0000")).toBe("cust_deadbeef0000");
    expect(evaluatorLabel(t, "Builtin.Helpfulness")).toBe("Helpfulness");
    setCustomEvaluatorNames({});
    expect(evaluatorLabel(t, "cust_f623957dd060")).toBe("cust_f623957dd060");
  });
});

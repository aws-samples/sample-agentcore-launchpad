import { describe, expect, it } from "vitest";

import ZH from "./builtin-scale-labels.zh.json";
import { displayBase, scaleLabelDisplay } from "./scale-labels";

// N17-obs428: display only — the stored label is the tooltip, never rewritten
describe("scaleLabelDisplay", () => {
  it("translates a built-in label per evaluator in zh", () => {
    expect(scaleLabelDisplay("Builtin.Helpfulness", "Very Helpful", "zh-CN")).toEqual({ text: "非常有帮助", original: "Very Helpful" });
    expect(scaleLabelDisplay("Builtin.Correctness", "Partially Correct", "zh-CN").text).toBe("部分正确");
    // Yes / No mean different things per evaluator
    expect(scaleLabelDisplay("Builtin.Refusal", "Yes", "zh-CN").text).toBe("已拒答");
    expect(scaleLabelDisplay("Builtin.GoalSuccessRate", "Yes", "zh-CN").text).toBe("目标达成");
    expect(scaleLabelDisplay("Builtin.ToolSelectionAccuracy", "No", "zh-CN").text).toBe("工具选择不当");
    expect(scaleLabelDisplay("Builtin.Harmfulness", "Not Harmful", "zh-CN").text).toBe("无害");
    expect(scaleLabelDisplay("Builtin.Faithfulness", "NOT_APPLICABLE", "zh-CN").text).toBe("不适用");
    expect(scaleLabelDisplay("Builtin.Helpfulness", "very helpful", "zh").text).toBe("非常有帮助");
  });

  it("keeps the stored label in en, for custom judges and for unknown labels", () => {
    expect(scaleLabelDisplay("Builtin.Helpfulness", "Very Helpful", "en")).toEqual({ text: "Very Helpful", original: null });
    expect(scaleLabelDisplay("cust_tone", "Yes", "zh-CN")).toEqual({ text: "Yes", original: null });
    expect(scaleLabelDisplay("Builtin.Helpfulness", "Brilliant", "zh-CN")).toEqual({ text: "Brilliant", original: null });
    expect(scaleLabelDisplay(null, "Yes", "zh-CN").original).toBeNull();
  });

  it("maps every entry of the table to a distinct zh text per evaluator", () => {
    for (const [id, labels] of Object.entries(ZH as Record<string, unknown>)) {
      if (id.startsWith("_") || id === "*") continue;
      const entries = Object.entries(labels as Record<string, string>).filter(([k]) => !["success", "failure"].includes(k));
      const shown = entries.map(([, zh]) => zh);
      expect(new Set(shown).size, id).toBe(shown.length);
      for (const [label] of entries) expect(scaleLabelDisplay(id, label, "zh-CN").original, `${id} ${label}`).toBe(label);
    }
  });
});

// a custom evaluator derived from a built-in (correctness_kimi = Correctness judged by Kimi)
describe("derived evaluators", () => {
  const derived = { base_evaluator_id: "Builtin.Correctness", scale_labels: [] };
  it("read through their base while the scale is the base's", () => {
    expect(displayBase(derived)).toBe("Builtin.Correctness");
    expect(scaleLabelDisplay("correctness_kimi", "Perfectly Correct", "zh-CN", derived))
      .toEqual({ text: "完全正确", original: "Perfectly Correct" });
    // a row listing the base's own labels is still the base's scale
    const listed = { base_evaluator_id: "Builtin.Correctness", scale_labels: ["Perfectly Correct", "Incorrect"] };
    expect(scaleLabelDisplay("correctness_kimi", "Incorrect", "zh-CN", listed).text).toBe("不正确");
    expect(scaleLabelDisplay("correctness_kimi", "Perfectly Correct", "en", derived).original).toBeNull();
  });

  it("keep an edited scale, and a plain custom judge, verbatim", () => {
    const edited = { base_evaluator_id: "Builtin.Correctness", scale_labels: ["Spot On", "Off"] };
    expect(displayBase(edited)).toBeNull();
    expect(scaleLabelDisplay("correctness_mine", "Spot On", "zh-CN", edited)).toEqual({ text: "Spot On", original: null });
    expect(scaleLabelDisplay("tone_judge", "Yes", "zh-CN", { base_evaluator_id: null, scale_labels: ["Yes", "No"] }).original).toBeNull();
    expect(displayBase({ base_evaluator_id: "ThirdParty.X.Y" })).toBeNull();
    expect(scaleLabelDisplay("correctness_kimi", "Perfectly Correct", "zh-CN", null).original).toBeNull();
  });
});

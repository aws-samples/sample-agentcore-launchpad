import { describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";

/** N17-obs310: the zh-CN outcome pair reads 通过 / 不通过 — no English "Bad Case" left in
 *  the outcome / insights family. */
describe("zh-CN outcome labels (N17-obs310)", () => {
  it("pairs 通过 with 不通过", () => {
    expect(zh.v2.outcome).toMatchObject({ passed: "通过", failed: "不通过", error: "评估异常" });
    expect(en.v2.outcome.failed).toBe("Bad Case");
  });

  /** N17-obs431: the pass bar is per evaluator (N17-obs425), so the 不通过 KPI must not
   *  promise a flat "< 0.7", and the outcome tag names the row's own bar. */
  it("states the per-evaluator pass bar (N17-obs431)", () => {
    for (const bundle of [zh, en]) {
      expect(bundle.v2.insights.kpiBadSub).not.toMatch(/低于 0\.7|below 0\.7/);
      expect(bundle.v2.insights.kpiBadSub).toMatch(/0\.7/);
      expect(bundle.v2.insights.kpiBadSub).toMatch(/1\.0/);
      expect(bundle.v2.outcome.barTitle).toContain("{{bar}}");
    }
    expect(zh.v2.insights.kpiBadSub).toContain("该评估器的通过线");
    expect(en.v2.insights.kpiBadSub).toContain("evaluator's own pass bar");
  });

  it("carries no English Bad Case anywhere in the zh-CN bundle", () => {
    expect(JSON.stringify(zh).toLowerCase()).not.toContain("bad case");
    expect(zh.v2.insights.kpiCountSub).toBe("{{passed}} 通过 · {{failed}} 不通过 · {{errors}} 异常");
  });
});

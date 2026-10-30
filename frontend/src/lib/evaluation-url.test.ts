import { describe, expect, it } from "vitest";

import { classicExperimentRedirect, v2EvaluationTarget, v2ExperimentsRedirect } from "./evaluation-url";

/**
 * N12-obs49 (user decision 2026-10-16, 隐藏掉配置实验，只保留金丝雀实验): the Experiments
 * module is canary-only in both consoles. Configuration-experiment deep links redirect to
 * the canary list; canary links keep working with or without the legacy `mode=canary`.
 */
describe("v2ExperimentsRedirect", () => {
  it("leaves canary URLs alone — the bare list, mode=canary, canary=new and canary=<id>", () => {
    expect(v2ExperimentsRedirect("")).toBeNull();
    expect(v2ExperimentsRedirect("?mode=canary")).toBeNull();
    expect(v2ExperimentsRedirect("?canary=new")).toBeNull();
    expect(v2ExperimentsRedirect("?mode=canary&canary=new&champion=a1")).toBeNull();
    expect(v2ExperimentsRedirect("?canary=can_42")).toBeNull();
  });

  it("sends every configuration-experiment URL to the canary list", () => {
    expect(v2ExperimentsRedirect("?mode=configuration")).toBe("/v2/eval/experiments");
    expect(v2ExperimentsRedirect("?view=new&agent=a1&lookback=24")).toBe("/v2/eval/experiments");
    expect(v2ExperimentsRedirect("?view=detail&id=exp_1")).toBe("/v2/eval/experiments");
    expect(v2ExperimentsRedirect("?id=exp_1")).toBe("/v2/eval/experiments");
  });
});

describe("classicExperimentRedirect", () => {
  it("keeps the canary sub-page and ignores other views", () => {
    expect(classicExperimentRedirect("?view=experiment&mode=canary")).toBeNull();
    expect(classicExperimentRedirect("?view=experiment&mode=canary&canary=can_1")).toBeNull();
    expect(classicExperimentRedirect("?view=online")).toBeNull();
    expect(classicExperimentRedirect("")).toBeNull();
  });

  it("turns the configuration sub-page (bare or exp=) into the canary sub-page", () => {
    expect(classicExperimentRedirect("?view=experiment")).toBe("/evaluation?view=experiment&mode=canary");
    expect(classicExperimentRedirect("?view=experiment&exp=new&agent=a1&baselineRun=r1")).toBe(
      "/evaluation?view=experiment&mode=canary",
    );
    expect(classicExperimentRedirect("?view=experiment&exp=exp_1")).toBe("/evaluation?view=experiment&mode=canary");
    // a canary id given without mode= still lands on that canary
    expect(classicExperimentRedirect("?view=experiment&canary=can_1")).toBe(
      "/evaluation?view=experiment&mode=canary&canary=can_1",
    );
  });
});

describe("v2EvaluationTarget", () => {
  it("maps online and new-run sub-pages onto their V2 twins", () => {
    expect(v2EvaluationTarget("?view=online")).toBe("/v2/eval/online");
    expect(v2EvaluationTarget("?view=online&oe=new")).toBe("/v2/eval/online?view=new");
    expect(v2EvaluationTarget("?view=online&oe=oc_1")).toBe("/v2/eval/online?view=detail&id=oc_1");
    expect(v2EvaluationTarget("?view=new&agent=a1&dataset=d1&evaluators=e1%2Ce2")).toBe(
      "/v2/eval/tasks?view=new&agent=a1&dataset=d1&evaluators=e1%2Ce2",
    );
    // the old "return to the experiment" exemption is gone with the configuration experiment
    expect(v2EvaluationTarget("?view=new&agent=a1&return=experiment")).toBe("/v2/eval/tasks?view=new&agent=a1");
  });

  it("maps the experiment sub-page onto the canary module, dropping exp= and keeping canary params", () => {
    expect(v2EvaluationTarget("?view=experiment")).toBe("/v2/eval/experiments");
    expect(v2EvaluationTarget("?view=experiment&exp=new&agent=a1&baselineRun=r1")).toBe("/v2/eval/experiments");
    expect(v2EvaluationTarget("?view=experiment&exp=exp_1")).toBe("/v2/eval/experiments");
    expect(v2EvaluationTarget("?view=experiment&mode=canary&canary=new&champion=a1&sourceExp=exp_1")).toBe(
      "/v2/eval/experiments?canary=new&champion=a1", // the retired promote hand-off id is dropped
    );
    expect(v2EvaluationTarget("?view=experiment&mode=canary&canary=can_1")).toBe("/v2/eval/experiments?canary=can_1");
  });

  it("keeps every other evaluation view classic", () => {
    expect(v2EvaluationTarget("")).toBeNull();
    expect(v2EvaluationTarget("?view=datasets")).toBeNull();
    expect(v2EvaluationTarget("?view=evaluators&ev=e1")).toBeNull();
  });
});

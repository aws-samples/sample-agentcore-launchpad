import { describe, expect, it } from "vitest";

import { ACTOR_MODELS, enabledProviders, evaluationRunPresentation, providerDisplayName, providerLabelFor, RUN_TERMINAL_STATUSES, runMeanScore } from "./evaluation";
import { evaluatorPolarity, JUDGE_MODEL_OPTIONS } from "./evaluators";
import { ALLOWED_MODEL_IDS, SPEC_DEFAULT_MODEL_ID } from "./models";
import { BAD_CASE_THRESHOLD, normalizedScore, scoreTone } from "../v2/format";

// N12 §A.4 / C3: the judge and actor-simulator pickers offer exactly the CN model
// catalog — GLM 5.3 (the backend default) first, Kimi K3 the only alternative.
describe("evaluation model pickers (CN catalog, C3)", () => {
  it("judge models are the Bedrock catalog ids, GLM 5.3 first", () => {
    expect(JUDGE_MODEL_OPTIONS).toEqual(ALLOWED_MODEL_IDS);
    expect(JUDGE_MODEL_OPTIONS[0]).toBe(SPEC_DEFAULT_MODEL_ID);
    expect(JUDGE_MODEL_OPTIONS).toEqual(["global.zai.glm-5.3", "global.moonshotai.kimi-k3"]);
  });
  it("actor-simulator models are the same two ids", () => {
    expect(ACTOR_MODELS).toEqual(JUDGE_MODEL_OPTIONS);
  });
  it("offers no model outside the catalog", () => {
    for (const id of [...JUDGE_MODEL_OPTIONS, ...ACTOR_MODELS]) {
      expect(id.startsWith("global.anthropic.") || id.startsWith("global.openai.")).toBe(false);
    }
  });
});

describe("run scoring helpers (restored verbatim from 4e27b1f4)", () => {
  it("runMeanScore flips penalty evaluators and weights by count", () => {
    expect(evaluatorPolarity("Builtin.Refusal")).toBe(-1);
    expect(evaluatorPolarity("Builtin.Helpfulness")).toBe(1);
    const mean = runMeanScore([
      { evaluatorId: "Builtin.Helpfulness", score: 0.8, count: 3 },
      { evaluatorId: "Builtin.Refusal", score: 0.9, count: 1 },
    ]);
    // (0.8 × 3 + (1 − 0.9) × 1) / 4
    expect(mean).toBeCloseTo((0.8 * 3 + 0.1) / 4, 6);
    expect(runMeanScore([])).toBeNull();
  });
  it("normalizedScore / scoreTone agree on the Bad Case threshold", () => {
    expect(normalizedScore(0.9, "Builtin.Refusal")).toBeCloseTo(0.1, 6);
    expect(scoreTone(BAD_CASE_THRESHOLD)).toBe("good");
    expect(scoreTone(0.5)).toBe("mid");
    expect(scoreTone(0.1)).toBe("bad");
  });
  it("a completed run with an error renders as completed_with_errors", () => {
    expect(evaluationRunPresentation({ status: "completed", error: "2 sessions failed" }).status).toBe(
      "completed_with_errors",
    );
    expect(evaluationRunPresentation({ status: "completed", error: null }).status).toBe("completed");
    expect(RUN_TERMINAL_STATUSES.has("completed")).toBe(true);
    expect(RUN_TERMINAL_STATUSES.has("evaluating")).toBe(false);
  });
});

// N12-obs43: the recommendation pickers list enabled providers only
describe("enabledProviders", () => {
  const base = { requires_source: true, supports: ["system_prompt"], models: [], default_model_id: null };
  it("drops providers the workspace switched off and keeps the rest in order", () => {
    const providers = [
      { ...base, id: "gepa_lite", label: "GEPA-lite", enabled: true },
      { ...base, id: "harness_optimizer", label: "Strands harness-optimizer", enabled: false },
    ];
    expect(enabledProviders(providers).map((p) => p.id)).toEqual(["gepa_lite"]);
  });
  it("treats a missing flag (older server) as enabled", () => {
    expect(enabledProviders([{ ...base, id: "gepa_lite", label: "GEPA-lite" }])).toHaveLength(1);
    expect(enabledProviders([])).toEqual([]);
  });
});

// N12-obs32: the console names the optimizer from the registry label — its head before
// the ` · ` that carries the method and executor — never the Global managed service.
describe("provider display names (N12-obs32)", () => {
  const base = { requires_source: true, supports: ["system_prompt"], models: [], default_model_id: null };
  const providers = [
    { ...base, id: "gepa_lite", label: "GEPA-lite · single reflective round (evaluation worker)" },
    { ...base, id: "harness_optimizer", label: "Strands harness-optimizer · contrastive reflection (evaluation worker)" },
  ];
  it("keeps the label's head and tolerates a label without a separator", () => {
    expect(providerDisplayName(providers[0].label)).toBe("GEPA-lite");
    expect(providerDisplayName(providers[1].label)).toBe("Strands harness-optimizer");
    expect(providerDisplayName("  Custom  ")).toBe("Custom");
  });
  it("resolves a stored provider id through the list and falls back to the id", () => {
    expect(providerLabelFor(providers, "harness_optimizer")).toBe("Strands harness-optimizer");
    expect(providerLabelFor(providers, "gepa_lite")).toBe("GEPA-lite");
    expect(providerLabelFor([], "gepa_lite")).toBe("gepa_lite");
    expect(providerLabelFor(providers, "agentcore")).toBe("agentcore");
  });
});

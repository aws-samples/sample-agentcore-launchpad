import { afterEach, describe, expect, it } from "vitest";
import { _resetModelCatalog, applyModelCatalog } from "../../lib/models";
import * as models from "./models";

const SF = "siliconflow.zai-org/GLM-5.3";
const sfRow = (available: boolean) => ({
  model_id: SF, label: "GLM 5.3 (SiliconFlow)", provider: "siliconflow",
  provider_kind: "openai_compatible" as const, provider_label: "SiliconFlow", tools: true, available,
});
const sfProvider = (configured: boolean) => ({
  siliconflow: { kind: "openai_compatible" as const, label: "SiliconFlow", configured,
    ssm_parameter: "launchpad-siliconflow-api-key" },
});

afterEach(() => _resetModelCatalog());

describe("studio canvas model catalog (spec C3, N-R6-obs2)", () => {
  it("is the platform catalog: the two Global models with GLM 5.3 first before capabilities load", () => {
    expect(models.studioModelOptions().map((m) => m.model_id)).toEqual([
      "global.zai.glm-5.3",
      "global.moonshotai.kimi-k3",
    ]);
    expect(models.DEFAULT_MODEL_ID).toBe("global.zai.glm-5.3");
    expect(models.defaultNewAgentModel().model_id).toBe("global.zai.glm-5.3");
    expect(models.isCustomModel("global.anthropic.claude-sonnet-5")).toBe(true);
  });

  it("offers the environment's OpenAI-compatible rows once capabilities are merged — a catalog id, not a custom one", () => {
    applyModelCatalog([sfRow(true)], sfProvider(true));
    expect(models.studioModelOptions().map((m) => m.model_id)).toContain(SF);
    expect(models.isCustomModel(SF)).toBe(false);
    expect(models.catalogModelLabel(SF)).toBe("GLM 5.3 (SiliconFlow)");
    expect(models.isModelAvailable(SF)).toBe(true);
    expect(models.modelUnavailableReason(SF)).toBeNull();
  });

  it("keeps an unconfigured provider's row listed but unavailable, and never defaults a new node onto it", () => {
    applyModelCatalog([sfRow(false)], sfProvider(false));
    const row = models.studioModelOptions().find((m) => m.model_id === SF);
    expect(row?.available).toBe(false);
    expect(models.isModelAvailable(SF)).toBe(false);
    expect(models.modelUnavailableReason(SF)).toEqual({
      provider: "SiliconFlow", parameter: "launchpad-siliconflow-api-key",
    });
    expect(models.modelOptionLabel(row!, "not configured")).toBe("GLM 5.3 (SiliconFlow) · not configured");
    expect(models.defaultNewAgentModel().model_id).toBe("global.zai.glm-5.3");
  });

  it("has no Mantle surface left", () => {
    expect(Object.keys(models).some((k) => /mantle/i.test(k))).toBe(false);
  });

  it("Anthropic detection gates the adaptive-thinking block (R17)", () => {
    expect(models.isAnthropicModel("global.anthropic.claude-sonnet-5")).toBe(true);
    expect(models.isAnthropicModel("global.zai.glm-5.3")).toBe(false);
    expect(models.isAnthropicModel("global.moonshotai.kimi-k3")).toBe(false);
    expect(models.isAnthropicModel(undefined)).toBe(false);
  });

  it("derives a model's display label from its id — never a hardcoded brand name (fix round 1, C5)", () => {
    expect(models.catalogModelLabel("global.zai.glm-5.3")).toBe("GLM 5.3 (Z.AI · global)");
    expect(models.catalogModelLabel("some.unknown.id")).toBe("some.unknown.id");
    expect(models.catalogModelLabel(undefined)).toBe(models.defaultNewAgentModel().label);
  });
});

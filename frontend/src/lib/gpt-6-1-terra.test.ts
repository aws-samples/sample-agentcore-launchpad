import { afterEach, describe, expect, it, vi } from "vitest";

import {
  gptBedrockModelConfig,
  isBedrockOpenAiGpt,
  setStudioModelContext,
  studioBedrockModels,
} from "../studio/lib/models";
import { api } from "./api";
import { resetModelCatalogForTests } from "./model-catalog";
import { loadModelDefaults, resetModelDefaultsForTests, STATIC_MODEL_DEFAULTS } from "./model-defaults";
import { MODEL_CATALOG, modelOptionsFor, supportsReasoningEffort } from "./models";
import { setActiveRegion } from "./regions";

const TERRA = "global.openai.gpt-6.1-terra";

afterEach(() => {
  vi.restoreAllMocks();
  resetModelDefaultsForTests();
  resetModelCatalogForTests();
  setActiveRegion(null);
  setStudioModelContext({});
});

describe("GPT-6.1 Terra", () => {
  it("is offered on native Bedrock through Converse, with a reasoning effort", () => {
    const entry = MODEL_CATALOG.bedrock.find((m) => m.model_id === TERRA);
    expect(entry).toEqual({ model_id: TERRA, label: "GPT-6.1 Terra (global)", api_format: "converse_stream" });
    expect(MODEL_CATALOG.mantle.map((m) => m.model_id)).not.toContain(TERRA);
    expect(supportsReasoningEffort(TERRA, "bedrock")).toBe(true);
    for (const region of ["us-east-1", "us-west-2"]) {
      setActiveRegion(region);
      expect(modelOptionsFor("bedrock").map((o) => o.model_id)).toContain(TERRA);
    }
  });

  it("leads the create form when the workspace default is set to it (prod env)", async () => {
    vi.spyOn(api, "modelDefaults").mockResolvedValue({
      ...STATIC_MODEL_DEFAULTS,
      agent_default_model_id: TERRA,
    });
    await loadModelDefaults();
    const first = modelOptionsFor("bedrock")[0];
    expect(first).toEqual({ model_id: TERRA, label: "GPT-6.1 Terra (global)", api_format: "converse_stream" });
  });

  it("is a Studio Bedrock model that takes the GPT request shape", () => {
    expect(studioBedrockModels().find((m) => m.model_id === TERRA)?.model_name).toBe("GPT-6.1 Terra (global)");
    expect(isBedrockOpenAiGpt(TERRA)).toBe(true);
    const code = gptBedrockModelConfig("agent", TERRA, 4096, true, "low", "");
    expect(code).toContain(`model_id="${TERRA}"`);
    expect(code).toContain('"effort": "low"');
    expect(code).not.toContain("thinking");
  });
});

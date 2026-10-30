import { describe, expect, it } from "vitest";
import * as models from "./models";

describe("platform model catalog (spec C3)", () => {
  it("offers exactly GLM 5.3 (default) and Kimi K3", () => {
    expect(models.MODEL_CATALOG.bedrock.map((o) => o.model_id)).toEqual([
      "global.zai.glm-5.3",
      "global.moonshotai.kimi-k3",
    ]);
    expect(models.ALLOWED_MODEL_IDS).toEqual(["global.zai.glm-5.3", "global.moonshotai.kimi-k3"]);
    expect(models.defaultModelFor("bedrock")).toBe("global.zai.glm-5.3");
    expect(models.SPEC_DEFAULT_MODEL_ID).toBe("global.zai.glm-5.3");
    expect(models.SPEC_DEFAULT_MODEL_REGION).toBe("us-west-2");
    expect(models.SPEC_DEFAULT_MODEL_CREDENTIAL).toBe("launchpad-global-bedrock");
    expect(models.modelOptionsFor("bedrock").every((o) => o.api_format === "converse_stream")).toBe(true);
  });

  it("treats every pre-CN id as custom and exports no Claude/Mantle helpers", () => {
    expect(models.isCustomModelId("global.anthropic.claude-sonnet-5", "bedrock")).toBe(true);
    expect(models.isCustomModelId("global.moonshotai.kimi-k3", "bedrock")).toBe(false);
    expect("CLAUDE_SDK_MODEL_SOURCE" in models).toBe(false);
    expect(Object.keys(models).some((k) => /mantle/i.test(k))).toBe(false);
  });
});

describe("live catalog from /api/capabilities (N-R6)", () => {
  const rows = [
    { model_id: "global.zai.glm-5.3", label: "GLM 5.3 (Z.AI · global)", provider: "bedrock",
      provider_kind: "bedrock" as const, provider_label: "Global Bedrock", tools: true, default: true },
    { model_id: "siliconflow.zai-org/GLM-5.3", label: "GLM-5.3 (SiliconFlow)", provider: "siliconflow",
      provider_kind: "openai_compatible" as const, provider_label: "SiliconFlow", tools: true },
    { model_id: "siliconflow.Qwen/Qwen3-8B", label: "Qwen3-8B (SiliconFlow)", provider: "siliconflow",
      provider_kind: "openai_compatible" as const, provider_label: "SiliconFlow", tools: false },
  ];

  it("merges the server rows once and keeps the Bedrock rows first", () => {
    models._resetModelCatalog();
    models.applyModelCatalog(rows);
    models.applyModelCatalog(rows); // idempotent
    expect(models.ALLOWED_MODEL_IDS).toEqual([
      "global.zai.glm-5.3", "global.moonshotai.kimi-k3",
      "siliconflow.zai-org/GLM-5.3", "siliconflow.Qwen/Qwen3-8B",
    ]);
    const sf = models.modelOption("siliconflow.zai-org/GLM-5.3");
    expect(sf?.provider_kind).toBe("openai_compatible");
    expect(sf?.api_format).toBe("chat_completions");
    expect(models.providerBadgeKey("siliconflow.zai-org/GLM-5.3")).toBe("models.provider.openai_compatible");
    expect(models.providerBadgeKey("global.zai.glm-5.3")).toBe("models.provider.bedrock");
    expect(models.providerBadgeKey("legacy.custom")).toBe("models.provider.bedrock");
    // agents get only tool-capable rows; judges the whole catalog
    expect(models.modelOptionsFor("bedrock").map((o) => o.model_id)).not.toContain("siliconflow.Qwen/Qwen3-8B");
    expect(models.modelOptionsFor("bedrock").map((o) => o.model_id)).toContain("siliconflow.zai-org/GLM-5.3");
    expect(models.judgeModelOptions().map((o) => o.model_id)).toContain("siliconflow.Qwen/Qwen3-8B");
    expect(models.isCustomModelId("siliconflow.Qwen/Qwen3-8B", "bedrock")).toBe(false);
    models._resetModelCatalog();
    expect(models.ALLOWED_MODEL_IDS).toEqual(["global.zai.glm-5.3", "global.moonshotai.kimi-k3"]);
  });
});

describe("unconfigured provider models (N-R6-obs1)", () => {
  const providers = {
    bedrock: { kind: "bedrock" as const, label: "Global Bedrock", configured: true,
      ssm_parameter: "launchpad-global-bedrock-api-key" },
    siliconflow: { kind: "openai_compatible" as const, label: "SiliconFlow", configured: false,
      ssm_parameter: "launchpad-siliconflow-api-key", api_key_env: "LAUNCHPAD_SILICONFLOW_API_KEY" },
  };
  const rows = [
    { model_id: "global.zai.glm-5.3", label: "GLM 5.3 (Z.AI · global)", provider: "bedrock",
      provider_kind: "bedrock" as const, provider_label: "Global Bedrock", tools: true, default: true, available: true },
    { model_id: "siliconflow.zai-org/GLM-5.3", label: "GLM-5.3 (SiliconFlow)", provider: "siliconflow",
      provider_kind: "openai_compatible" as const, provider_label: "SiliconFlow", tools: true, available: false },
  ];

  it("carries the flag, names the SSM parameter and never defaults to an unavailable row", () => {
    models._resetModelCatalog();
    models.applyModelCatalog(rows, providers);
    expect(models.modelOption("siliconflow.zai-org/GLM-5.3")?.available).toBe(false);
    expect(models.isModelAvailable("siliconflow.zai-org/GLM-5.3")).toBe(false);
    expect(models.isModelAvailable("global.zai.glm-5.3")).toBe(true);
    expect(models.isModelAvailable("legacy.custom")).toBe(true); // the backend's catalog check refuses it
    expect(models.MODEL_PROVIDERS.siliconflow).toEqual({
      kind: "openai_compatible", label: "SiliconFlow", configured: false,
      ssm_parameter: "launchpad-siliconflow-api-key",
    });
    expect(models.modelUnavailableReason("siliconflow.zai-org/GLM-5.3")).toEqual({
      provider: "SiliconFlow", parameter: "launchpad-siliconflow-api-key",
    });
    expect(models.modelUnavailableReason("global.zai.glm-5.3")).toBeNull();
    expect(models.modelUnavailableReason("legacy.custom")).toBeNull();
    // the unavailable row stays listed (disabled by the pickers), the default skips it
    expect(models.modelOptionsFor("bedrock").map((o) => o.model_id)).toContain("siliconflow.zai-org/GLM-5.3");
    expect(models.defaultModelFor("bedrock")).toBe("global.zai.glm-5.3");
    const sf = models.modelOption("siliconflow.zai-org/GLM-5.3")!;
    expect(models.modelOptionLabel(sf, "not configured")).toBe("GLM-5.3 (SiliconFlow) · not configured");
    expect(models.modelOptionLabel(sf, "not configured", true)).toBe("GLM-5.3 (SiliconFlow) · siliconflow.zai-org/GLM-5.3 · not configured");
    expect(models.modelOptionLabel(models.modelOption("global.zai.glm-5.3")!, "not configured")).toBe("GLM 5.3 (Z.AI · global)");
    models._resetModelCatalog();
    expect(models.MODEL_PROVIDERS).toEqual({ bedrock: { kind: "bedrock", label: "Global Bedrock", configured: true,
      ssm_parameter: "launchpad-global-bedrock-api-key" } });
  });

  it("skips the first rows when they are unavailable and treats an older server as available", () => {
    models._resetModelCatalog();
    // every Bedrock row unavailable (hypothetical) → the first available wins; none → the first
    models.applyModelCatalog([
      { ...rows[0], available: false },
      { model_id: "global.moonshotai.kimi-k3", label: "Kimi K3", provider: "bedrock", provider_kind: "bedrock",
        provider_label: "Global Bedrock", tools: true, available: false },
      { ...rows[1], available: true },
    ], { ...providers, siliconflow: { ...providers.siliconflow, configured: true } });
    expect(models.defaultModelFor("bedrock")).toBe("siliconflow.zai-org/GLM-5.3");
    models._resetModelCatalog();
    // an older server: no `available`, no `model_providers` → nothing is disabled
    models.applyModelCatalog([{ ...rows[1], available: undefined }]);
    expect(models.isModelAvailable("siliconflow.zai-org/GLM-5.3")).toBe(true);
    expect(models.modelUnavailableReason("siliconflow.zai-org/GLM-5.3")).toBeNull();
    // the provider row without the flag is configured; Bedrock always is
    models.applyModelCatalog([], { siliconflow: { kind: "openai_compatible", label: "SiliconFlow" },
      bedrock: { kind: "bedrock", label: "Global Bedrock", configured: false } });
    expect(models.MODEL_PROVIDERS.siliconflow.configured).toBe(true);
    expect(models.MODEL_PROVIDERS.siliconflow.ssm_parameter).toBe("launchpad-siliconflow-api-key");
    expect(models.MODEL_PROVIDERS.bedrock.configured).toBe(true);
    models._resetModelCatalog();
  });
});

describe("D21 additive catalog keys", () => {
  it("carries source / kind onto the merged option and leaves older servers alone", () => {
    models._resetModelCatalog();
    models.applyModelCatalog(
      [
        { model_id: "global.zai.glm-5.3", label: "GLM 5.3", provider: "bedrock", provider_kind: "bedrock",
          provider_label: "Global Bedrock", tools: true, source: "builtin", kind: "chat", enabled: true },
        { model_id: "siliconflow.Qwen/Qwen3-8B", label: "Qwen3-8B", provider: "siliconflow",
          provider_kind: "openai_compatible", provider_label: "SiliconFlow", tools: true, source: "probed", kind: "chat" },
        { model_id: "global.moonshotai.kimi-k3", label: "Kimi K3", provider: "bedrock", provider_kind: "bedrock",
          provider_label: "Global Bedrock", tools: true },
      ],
      { siliconflow: { kind: "openai_compatible", label: "SiliconFlow", configured: true, id: "mp_sf", enabled: true, probe_state: "ok" } },
    );
    expect(models.modelOption("global.zai.glm-5.3")?.source).toBe("builtin");
    expect(models.modelOption("siliconflow.Qwen/Qwen3-8B")?.source).toBe("probed");
    expect(models.modelOption("siliconflow.Qwen/Qwen3-8B")?.kind).toBe("chat");
    expect(models.modelOption("global.moonshotai.kimi-k3")?.source).toBeUndefined();
    // the existing picker surface is unchanged
    expect(models.modelOptionsFor("bedrock").map((o) => o.model_id)).toEqual([
      "global.zai.glm-5.3", "global.moonshotai.kimi-k3", "siliconflow.Qwen/Qwen3-8B",
    ]);
    expect(models.isModelAvailable("siliconflow.Qwen/Qwen3-8B")).toBe(true);
    models._resetModelCatalog();
  });
});

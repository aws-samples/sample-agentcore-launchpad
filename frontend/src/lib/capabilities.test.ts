import { HIDDEN_SURFACES } from "../v2/hiddenSurfaces";
import { describe, expect, it } from "vitest";

import {
  ALL_FEATURES,
  _resetFeaturesCache,
  featureForPath,
  hasFeature,
  loadCapabilities,
  loadFeatures,
  parseCapabilities,
} from "./capabilities";
import { ALLOWED_MODEL_IDS, _resetModelCatalog, isModelAvailable, MODEL_PROVIDERS, modelUnavailableReason } from "./models";

describe("capabilities", () => {
  it("maps paths to features", () => {
    expect(featureForPath("/v2/knowledge-bases")).toBe("knowledge_bases");
    expect(featureForPath("/governance")).toBe("gateway");
    expect(featureForPath("/v2/connections")).toBe("identity");
    // N12: both navigations hide the Evaluation group when the feature is absent
    expect(featureForPath("/v2/eval/tasks")).toBe("evaluation");
    expect(featureForPath("/evaluation")).toBe("evaluation");
    expect(ALL_FEATURES).toContain("optimization");
    expect(featureForPath("/v2/skill-lab")).toBe("skill_lab");
    // video library (CN port of the upstream module): both pages follow the `videos` flag
    expect(featureForPath("/videos")).toBe("videos");
    expect(featureForPath("/v2/videos")).toBe("videos");
    expect(featureForPath("/v2/video-management")).toBe("videos");
    expect(featureForPath("/")).toBeNull();
    expect(featureForPath("/announcements")).toBeNull();
  });
  it("parses the per-method creation-time domain and tolerates an older server", () => {
    const parsed = parseCapabilities({
      features: ["agents"],
      methods: { harness: { builtin_tools: ["browser"], protocols: ["http"], knowledge_bases: true }, byoc: { memory: true, knowledge_bases: false, byoc_kinds: ["code_zip"] } },
    });
    expect(parsed.methods?.harness.builtin_tools).toEqual(["browser"]);
    expect(parsed.methods?.harness.native_tools).toEqual([]); // missing list → empty, never undefined
    expect(parsed.methods?.harness.memory).toBe(true); // missing flag → supported
    expect(parsed.methods?.byoc.knowledge_bases).toBe(false);
    expect(parseCapabilities({ features: ["agents"] }).methods).toBeNull();
  });
  it("hasFeature is permissive while loading", () => {
    expect(hasFeature(null, "memory")).toBe(true);
    expect(hasFeature(new Set(["agents"]), "memory")).toBe(false);
  });
  it("drops unknown features from the server list", async () => {
    _resetFeaturesCache();
    const fetcher = (async () =>
      new Response(JSON.stringify({ features: ["agents", "holograms"] }))) as unknown as typeof fetch;
    const set = await loadFeatures(fetcher);
    expect([...set]).toEqual(["agents"]);
    _resetFeaturesCache();
  });
  it("falls back to every feature when the fetch itself fails", async () => {
    _resetFeaturesCache();
    const fetcher = (async () => {
      throw new Error("network error");
    }) as unknown as typeof fetch;
    const set = await loadFeatures(fetcher);
    expect(set).toEqual(new Set(ALL_FEATURES.filter((f) => !HIDDEN_SURFACES.features.includes(f))));
    _resetFeaturesCache();
  });
  it("falls back to every feature on a non-ok response", async () => {
    _resetFeaturesCache();
    const fetcher = (async () => new Response("", { status: 500 })) as unknown as typeof fetch;
    const set = await loadFeatures(fetcher);
    expect(set).toEqual(new Set(ALL_FEATURES.filter((f) => !HIDDEN_SURFACES.features.includes(f))));
    _resetFeaturesCache();
  });
  // N12-obs41: the Lambda "Code" evaluator definition follows the server's opt-in
  it("reads the code-evaluator opt-in from the evaluation block", () => {
    expect(parseCapabilities({ features: ["evaluation"], evaluation: { code_evaluators_enabled: true } }))
      .toEqual({ features: new Set(["evaluation"]), methods: null, codeEvaluatorsEnabled: true, policy: null, browserSigning: false });
    expect(parseCapabilities({ features: [], evaluation: { code_evaluators_enabled: false } }).codeEvaluatorsEnabled).toBe(false);
    // older server without the block: off, never on by default
    expect(parseCapabilities({ features: ["agents"] }).codeEvaluatorsEnabled).toBe(false);
  });
  // N17-obs258: Web Bot Auth is offered only where the server says the partition signs
  it("reads browser signing from the tools block and never assumes it", async () => {
    expect(parseCapabilities({ features: [], tools: { browser_signing: true } }).browserSigning).toBe(true);
    expect(parseCapabilities({ features: [], tools: { browser_signing: false } }).browserSigning).toBe(false);
    expect(parseCapabilities({ features: [] }).browserSigning).toBe(false);
    _resetFeaturesCache();
    const fetcher = (async () => {
      throw new Error("network error");
    }) as unknown as typeof fetch;
    expect((await loadCapabilities(fetcher)).browserSigning).toBe(false);
    _resetFeaturesCache();
  });
  it("never assumes the opt-in when the fetch fails", async () => {
    _resetFeaturesCache();
    const fetcher = (async () => {
      throw new Error("network error");
    }) as unknown as typeof fetch;
    const caps = await loadCapabilities(fetcher);
    expect(caps.codeEvaluatorsEnabled).toBe(false);
    expect(caps.features).toEqual(new Set(ALL_FEATURES.filter((f) => !HIDDEN_SURFACES.features.includes(f))));
    _resetFeaturesCache();
  });
  it("caches one capabilities fetch for features and opt-ins alike", async () => {
    _resetFeaturesCache();
    let calls = 0;
    const fetcher = (async () => {
      calls += 1;
      return new Response(JSON.stringify({ features: ["evaluation"], evaluation: { code_evaluators_enabled: true } }));
    }) as unknown as typeof fetch;
    expect(await loadFeatures(fetcher)).toEqual(new Set(["evaluation"]));
    expect((await loadCapabilities(fetcher)).codeEvaluatorsEnabled).toBe(true);
    expect(calls).toBe(1);
    _resetFeaturesCache();
  });
  // the Policy module (its own nav entry, /v2/policy) follows the `policy` feature; the
  // `{engines, attached_gateways}` block is read only when the feature is on
  it("reads the policy capability block only with the policy feature", () => {
    expect(ALL_FEATURES).toContain("policy");
    const on = parseCapabilities({
      features: ["gateway", "policy"],
      policy: { engines: 1, attached_gateways: 2 },
    });
    expect(on.features.has("policy")).toBe(true);
    expect(on.policy).toEqual({ engines: 1, attachedGateways: 2 });
    // feature off: the block is ignored even if a server sent one
    expect(parseCapabilities({ features: ["gateway"], policy: { engines: 3 } }).policy).toBeNull();
    // feature on, empty block: zero counts
    expect(parseCapabilities({ features: ["policy"], policy: {} }).policy).toEqual({ engines: 0, attachedGateways: 0 });
    expect(parseCapabilities({ features: ["policy"] }).policy).toBeNull();
    expect(featureForPath("/v2/policy")).toBe("policy");
    // the Governance route itself stays gateway-gated (plan §3.2)
    expect(featureForPath("/v2/governance")).toBe("gateway");
  });
  it("never assumes a policy posture when the fetch fails", async () => {
    _resetFeaturesCache();
    const fetcher = (async () => {
      throw new Error("network error");
    }) as unknown as typeof fetch;
    expect((await loadCapabilities(fetcher)).policy).toBeNull();
    _resetFeaturesCache();
  });
  it("merges the model catalog rows into lib/models (N-R6)", () => {
    _resetModelCatalog();
    parseCapabilities({
      features: ["agents"],
      models: [{ model_id: "siliconflow.Qwen/Qwen3.8-27B", label: "Qwen3.8-27B (SiliconFlow)", provider: "siliconflow",
                 provider_kind: "openai_compatible", provider_label: "SiliconFlow", tools: true }],
    });
    expect(ALLOWED_MODEL_IDS).toContain("siliconflow.Qwen/Qwen3.8-27B");
    // an older server without the block leaves the static catalog alone
    _resetModelCatalog();
    parseCapabilities({ features: ["agents"] });
    expect(ALLOWED_MODEL_IDS).toEqual(["global.zai.glm-5.3", "global.moonshotai.kimi-k3"]);
  });
  it("marks the models of an unconfigured provider unavailable with the SSM parameter (N-R6-obs1)", () => {
    _resetModelCatalog();
    parseCapabilities({
      features: ["agents"],
      models: [
        { model_id: "global.zai.glm-5.3", label: "GLM 5.3", provider: "bedrock", provider_kind: "bedrock",
          provider_label: "Global Bedrock", tools: true, default: true, available: true },
        { model_id: "siliconflow.Qwen/Qwen3.8-27B", label: "Qwen3.8-27B (SiliconFlow)", provider: "siliconflow",
          provider_kind: "openai_compatible", provider_label: "SiliconFlow", tools: true, available: false },
      ],
      model_providers: {
        bedrock: { kind: "bedrock", label: "Global Bedrock", credential_provider: "launchpad-global-bedrock",
          configured: true, ssm_parameter: "launchpad-global-bedrock-api-key", api_key_env: "LAUNCHPAD_GLOBAL_BEDROCK_API_KEY" },
        siliconflow: { kind: "openai_compatible", label: "SiliconFlow", credential_provider: "launchpad-siliconflow",
          configured: false, ssm_parameter: "launchpad-siliconflow-api-key", api_key_env: "LAUNCHPAD_SILICONFLOW_API_KEY" },
      },
    });
    expect(isModelAvailable("siliconflow.Qwen/Qwen3.8-27B")).toBe(false);
    expect(isModelAvailable("global.zai.glm-5.3")).toBe(true);
    expect(MODEL_PROVIDERS.siliconflow.configured).toBe(false);
    expect(modelUnavailableReason("siliconflow.Qwen/Qwen3.8-27B")).toEqual({
      provider: "SiliconFlow", parameter: "launchpad-siliconflow-api-key",
    });
    // the key material itself is never part of the payload the client reads
    expect(JSON.stringify(MODEL_PROVIDERS)).not.toMatch(/api_key_env|LAUNCHPAD_/);
    _resetModelCatalog();
  });
});

describe("console-side hidden features (phase 2)", () => {
  it("drops skill_lab from the server's feature list so nav, home and routes treat it as absent", () => {
    const caps = parseCapabilities({ features: [...ALL_FEATURES] } as never);
    expect(HIDDEN_SURFACES.features).toContain("skill_lab");
    expect(caps.features.has("skill_lab")).toBe(false);
    expect(caps.features.has("agents")).toBe(true);
  });
});

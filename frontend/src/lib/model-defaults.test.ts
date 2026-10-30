import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { emptyAgentForm } from "./agent-spec";
import type { ModelDefaultsOut, ModelRoleSetting } from "./api";
import {
  adoptModelDefault,
  currentModelDefaults,
  loadModelDefaults,
  MODEL_ROLES,
  normalizeModelDefaults,
  reloadModelDefaults,
  resetModelDefaultsForTests,
  type RoleCatalogRow,
  roleModelOptions,
  STATIC_MODEL_DEFAULTS,
  withCurrentModel,
} from "./model-defaults";
import { _resetModelCatalog, defaultModelFor } from "./models";

/**
 * r15-e (upstream #275 subset): the pickers seed the platform's configured model per role
 * from `GET /api/model-defaults` instead of a hard-coded id, fall back to the built-in
 * defaults when the fetch fails, and never rewrite a stored or member-picked value.
 */

const GLM = "global.zai.glm-5.3";
const KIMI = "global.moonshotai.kimi-k3";

const modelDefaults = vi.hoisted(() => vi.fn<() => Promise<ModelDefaultsOut>>());
vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, api: { ...actual.api, modelDefaults } };
});

function role(partial: Partial<ModelRoleSetting> & { role: string }): ModelRoleSetting {
  return {
    field: `${partial.role}_model_id`, model_id: GLM, default: GLM, overridden_by: null, updated_at: null,
    needs_tools: false, allow_empty: false, ...partial,
  };
}

beforeEach(() => {
  resetModelDefaultsForTests();
  _resetModelCatalog();
  modelDefaults.mockReset();
});
afterEach(() => {
  resetModelDefaultsForTests();
  _resetModelCatalog();
});

describe("static defaults", () => {
  it("are today's config: GLM 5.3 everywhere, Kimi K3 for the Skill Lab target, no judge fallback", () => {
    expect(MODEL_ROLES).toEqual(["agent_default", "judge", "judge_fallback", "failure_detection", "actor", "prompt_opt", "skill_lab_judge", "skill_lab_target", "memory_extract", "codegen", "embedding"]);
    expect(STATIC_MODEL_DEFAULTS.failure_detection).toBe(KIMI); // N17-obs449
    expect(STATIC_MODEL_DEFAULTS.embedding).toBe("amazon.titan-embed-text-v2:0"); // N17-obs252
    expect(STATIC_MODEL_DEFAULTS.agent_default).toBe(GLM);
    expect(STATIC_MODEL_DEFAULTS.skill_lab_target).toBe(KIMI);
    expect(STATIC_MODEL_DEFAULTS.judge_fallback).toBe("");
    expect(currentModelDefaults()).toEqual(STATIC_MODEL_DEFAULTS);
  });
});

describe("normalizeModelDefaults", () => {
  it("takes the server's model per known role and keeps the static one otherwise", () => {
    const out = normalizeModelDefaults({
      roles: [
        role({ role: "judge", model_id: KIMI }),
        role({ role: "judge_fallback", model_id: "", allow_empty: true }),
        role({ role: "actor", model_id: "   " }), // blank on a role that needs a model: static
        role({ role: "assistant", model_id: KIMI }), // unknown role: ignored
        { role: "prompt_opt" } as unknown as ModelRoleSetting, // missing model_id: static
      ],
    });
    expect(out.judge).toBe(KIMI);
    expect(out.judge_fallback).toBe("");
    expect(out.actor).toBe(GLM);
    expect(out.prompt_opt).toBe(GLM);
    expect(Object.keys(out)).toEqual(MODEL_ROLES);
    expect(normalizeModelDefaults(null)).toEqual(STATIC_MODEL_DEFAULTS);
    expect(normalizeModelDefaults({ roles: "nope" })).toEqual(STATIC_MODEL_DEFAULTS);
  });
});

describe("loadModelDefaults", () => {
  it("fetches once per page load and shares the answer", async () => {
    modelDefaults.mockResolvedValue({ roles: [role({ role: "agent_default", model_id: KIMI })] });
    const [a, b] = await Promise.all([loadModelDefaults(), loadModelDefaults()]);
    expect(a).toBe(b);
    expect(a.agent_default).toBe(KIMI);
    expect(modelDefaults).toHaveBeenCalledTimes(1);
    expect(currentModelDefaults().agent_default).toBe(KIMI);
    // a reload refetches
    modelDefaults.mockResolvedValue({ roles: [role({ role: "agent_default", model_id: GLM })] });
    expect((await reloadModelDefaults()).agent_default).toBe(GLM);
    expect(modelDefaults).toHaveBeenCalledTimes(2);
  });

  it("falls back to the static defaults on a failed fetch without caching the failure", async () => {
    modelDefaults.mockRejectedValueOnce(new Error("offline"));
    expect(await loadModelDefaults()).toEqual(STATIC_MODEL_DEFAULTS);
    expect(currentModelDefaults()).toEqual(STATIC_MODEL_DEFAULTS);
    modelDefaults.mockResolvedValue({ roles: [role({ role: "judge", model_id: KIMI })] });
    expect((await loadModelDefaults()).judge).toBe(KIMI); // the next mount retried
    expect(modelDefaults).toHaveBeenCalledTimes(2);
  });
});

describe("adoptModelDefault / withCurrentModel", () => {
  it("replaces only the value the form seeded", () => {
    expect(adoptModelDefault(GLM, GLM, KIMI)).toBe(KIMI); // still the seed → configured
    expect(adoptModelDefault(KIMI, GLM, "siliconflow.x")).toBe(KIMI); // the member's pick stays
    expect(adoptModelDefault("stored.model", GLM, KIMI)).toBe("stored.model"); // a stored value stays
    expect(adoptModelDefault(GLM, GLM, "")).toBe(GLM); // an empty configured value changes nothing
  });

  it("keeps a configured or stored id in a closed option list", () => {
    expect(withCurrentModel([GLM, KIMI], KIMI)).toEqual([GLM, KIMI]);
    expect(withCurrentModel([GLM, KIMI], "siliconflow.x")).toEqual(["siliconflow.x", GLM, KIMI]);
    expect(withCurrentModel([GLM], "")).toEqual([GLM]);
    expect(withCurrentModel([GLM], null)).toEqual([GLM]);
  });
});

describe("roleModelOptions", () => {
  const catalog: RoleCatalogRow[] = [
    { model_id: GLM, label: "GLM", kind: "chat", enabled: true, tools: true, provider_enabled: true },
    { model_id: KIMI, label: "Kimi", kind: "chat", enabled: true, tools: true, provider_enabled: true },
    { model_id: "siliconflow.plain", label: "Plain", kind: "chat", enabled: true, tools: false, provider_enabled: true },
    { model_id: "siliconflow.off", label: "Off", kind: "chat", enabled: false, tools: true, provider_enabled: true },
    { model_id: "acme.gone", label: "Gone", kind: "chat", enabled: true, tools: true, provider_enabled: false },
    { model_id: "amazon.titan-embed-text-v2:0", label: "Titan", kind: "embedding", enabled: true, tools: false, provider_enabled: true },
  ];

  it("offers enabled chat rows only, tool-capable where the role runs tools, current + default first", () => {
    expect(roleModelOptions({ model_id: GLM, default: GLM, needs_tools: true }, catalog)).toEqual([GLM, KIMI]);
    expect(roleModelOptions({ model_id: KIMI, default: GLM, needs_tools: false }, catalog)).toEqual([KIMI, GLM, "siliconflow.plain"]);
    // a current / default id the catalog no longer offers is never dropped
    expect(roleModelOptions({ model_id: "siliconflow.off", default: "legacy.id", needs_tools: true }, catalog)).toEqual(["siliconflow.off", "legacy.id", GLM, KIMI]);
    expect(roleModelOptions({ model_id: "", default: "", needs_tools: false }, catalog)).toEqual([GLM, KIMI, "siliconflow.plain"]);
  });
});

describe("one picker seed: the Create Agent form", () => {
  it("seeds the configured agent default when it is an available catalog option, else the first available", () => {
    expect(emptyAgentForm("harness").modelId).toBe(GLM);
    resetModelDefaultsForTests({ agent_default: KIMI });
    expect(defaultModelFor("bedrock")).toBe(KIMI);
    expect(emptyAgentForm("harness").modelId).toBe(KIMI);
    expect(emptyAgentForm("zip_runtime").modelId).toBe(KIMI);
    // a configured id the catalog does not offer (or is unavailable) cannot be seeded
    resetModelDefaultsForTests({ agent_default: "siliconflow.not-loaded" });
    expect(emptyAgentForm("harness").modelId).toBe(GLM);
  });
});

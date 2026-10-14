import { afterEach, describe, expect, it, vi } from "vitest";

import { defaultModelForMethod, emptyAgentForm } from "./agent-spec";
import { api, type ModelRole, type ModelRoleSetting } from "./api";
import { proposalDefaultModel, proposalDraftFrom } from "./assistant";
import {
  adoptModelDefault,
  currentModelDefaults,
  defaultModelForRole,
  loadModelDefaults,
  type ModelDefaults,
  modelFamily,
  normalizeModelDefaults,
  reloadModelDefaults,
  resetModelDefaultsForTests,
  roleAccepts,
  roleModelOptions,
  STATIC_MODEL_DEFAULTS,
  withCurrentModel,
} from "./model-defaults";
import { MODEL_CATALOG } from "./models";
import { DEFAULT_MODEL_ID, studioFallbackModelId } from "../studio/lib/models";

/** A server answer: every role, with `overrides` applied. */
function answer(overrides: Partial<ModelDefaults> = {}): { roles: ModelRoleSetting[] } {
  return {
    roles: (Object.keys(STATIC_MODEL_DEFAULTS) as ModelRole[]).map((role) => ({
      role,
      model_id: overrides[role] ?? STATIC_MODEL_DEFAULTS[role],
      default: STATIC_MODEL_DEFAULTS[role],
      overridden: role in overrides,
      constraint: null,
      allow_empty: role === "judge_fallback_model_id",
      needs_tools: false,
      updated_by: null,
      updated_at: null,
    })),
  };
}

// An administrator moved several roles on the Models page.
const CONFIGURED: Partial<ModelDefaults> = {
  agent_default_model_id: "us.amazon.nova-pro-v1:0",
  claude_sdk_model_id: "global.anthropic.claude-sonnet-4-6",
  judge_model_id: "us.amazon.nova-premier-v1:0",
  assistant_model_id: "global.moonshotai.kimi-k3",
  actor_model_id: "global.anthropic.claude-haiku-5-5",
  studio_fallback_model_id: "us.amazon.nova-lite-v1:0",
};

afterEach(() => {
  vi.restoreAllMocks();
  resetModelDefaultsForTests();
});

describe("static defaults", () => {
  it("equal the backend's built-in values (backend/app/core/config.py)", () => {
    expect(STATIC_MODEL_DEFAULTS).toEqual({
      agent_default_model_id: "global.openai.gpt-6-sol",
      claude_sdk_model_id: "global.anthropic.claude-sonnet-5",
      judge_model_id: "global.openai.gpt-6-sol",
      judge_fallback_model_id: "global.anthropic.claude-sonnet-5-5",
      assistant_model_id: "global.openai.gpt-6-sol",
      architect_model_id: "global.openai.gpt-6-astra",
      actor_model_id: "global.openai.gpt-6-luna",
      studio_fallback_model_id: "global.anthropic.claude-sonnet-5",
      codegen_model: "global.anthropic.claude-sonnet-5",
      prompt_opt_default_model_id: "global.anthropic.claude-opus-5",
      skill_lab_judge_model_id: "us.openai.gpt-6-sol",
      skill_lab_target_model_id: "global.anthropic.claude-opus-5-5",
      skill_lab_codex_target_model_id: "global.openai.gpt-6-sol",
    });
  });

  it("are what every helper resolves to before the server answers", () => {
    expect(currentModelDefaults()).toEqual(STATIC_MODEL_DEFAULTS);
    expect(defaultModelForMethod("harness")).toBe("global.openai.gpt-6-sol");
    expect(defaultModelForMethod("container")).toBe("global.anthropic.claude-sonnet-5");
    expect(studioFallbackModelId()).toBe(DEFAULT_MODEL_ID);
    expect(proposalDefaultModel("bedrock")).toBe("global.openai.gpt-6-sol");
  });
});

describe("loader", () => {
  it("fetches once and serves the cache", async () => {
    const spy = vi.spyOn(api, "modelDefaults").mockResolvedValue(answer(CONFIGURED));
    const [a, b] = await Promise.all([loadModelDefaults(), loadModelDefaults()]);
    await loadModelDefaults();
    expect(spy).toHaveBeenCalledTimes(1);
    expect(b).toBe(a);
    expect(currentModelDefaults().judge_model_id).toBe("us.amazon.nova-premier-v1:0");
  });

  it("falls back to the static values on error, without caching, so the next load retries", async () => {
    const spy = vi.spyOn(api, "modelDefaults").mockRejectedValueOnce(new Error("boom"));
    expect(await loadModelDefaults()).toEqual(STATIC_MODEL_DEFAULTS);
    expect(currentModelDefaults()).toEqual(STATIC_MODEL_DEFAULTS);
    spy.mockResolvedValueOnce(answer(CONFIGURED));
    expect((await loadModelDefaults()).actor_model_id).toBe(CONFIGURED.actor_model_id);
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("refetches after an administrator's change", async () => {
    vi.spyOn(api, "modelDefaults")
      .mockResolvedValueOnce(answer())
      .mockResolvedValueOnce(answer({ judge_model_id: "us.amazon.nova-pro-v1:0" }));
    await loadModelDefaults();
    expect(defaultModelForRole("judge_model_id")).toBe(STATIC_MODEL_DEFAULTS.judge_model_id);
    await reloadModelDefaults();
    expect(defaultModelForRole("judge_model_id")).toBe("us.amazon.nova-pro-v1:0");
  });

  it("keeps the static value for a blank or missing role, but honours an empty judge fallback", () => {
    const raw = answer({ judge_model_id: "  us.amazon.nova-pro-v1:0 ", actor_model_id: "", judge_fallback_model_id: "" });
    raw.roles = raw.roles.filter((r) => r.role !== "architect_model_id");
    const out = normalizeModelDefaults(raw);
    expect(out.judge_model_id).toBe("us.amazon.nova-pro-v1:0");
    expect(out.actor_model_id).toBe(STATIC_MODEL_DEFAULTS.actor_model_id);
    expect(out.judge_fallback_model_id).toBe("");
    expect(out.architect_model_id).toBe(STATIC_MODEL_DEFAULTS.architect_model_id);
    expect(normalizeModelDefaults(null)).toEqual(STATIC_MODEL_DEFAULTS);
    expect(normalizeModelDefaults({ roles: [{ role: "nope", model_id: "x.y" }] })).toEqual(STATIC_MODEL_DEFAULTS);
  });
});

describe("pickers follow the server", () => {
  it("the agent default, and the Claude SDK default for the container method", async () => {
    vi.spyOn(api, "modelDefaults").mockResolvedValue(answer(CONFIGURED));
    const defaults = await loadModelDefaults();
    expect(defaultModelForRole("agent_default_model_id")).toBe("us.amazon.nova-pro-v1:0");
    expect(defaultModelForRole("agent_default_model_id", { method: "container" })).toBe("global.anthropic.claude-sonnet-4-6");
    expect(defaultModelForRole("agent_default_model_id", { method: "harness", defaults: STATIC_MODEL_DEFAULTS })).toBe(
      "global.openai.gpt-6-sol",
    );
    expect(defaultModelForMethod("harness")).toBe("us.amazon.nova-pro-v1:0");
    expect(defaultModelForMethod("zip_runtime", "bedrock", defaults)).toBe("us.amazon.nova-pro-v1:0");
    expect(defaultModelForMethod("container")).toBe("global.anthropic.claude-sonnet-4-6");
    expect(emptyAgentForm("harness").modelId).toBe("us.amazon.nova-pro-v1:0");
    expect(emptyAgentForm("byoc").byocModels).toEqual(["us.amazon.nova-pro-v1:0"]);
  });

  it("a Mantle source keeps its catalog default (a Bedrock id is not a Mantle id)", async () => {
    vi.spyOn(api, "modelDefaults").mockResolvedValue(answer(CONFIGURED));
    await loadModelDefaults();
    expect(defaultModelForMethod("harness", "mantle")).toBe(MODEL_CATALOG.mantle[0].model_id);
    expect(proposalDefaultModel("mantle")).toBe(MODEL_CATALOG.mantle[0].model_id);
  });

  it("the Studio fallback is its own role, independent of the agent default", async () => {
    vi.spyOn(api, "modelDefaults").mockResolvedValue(answer({ agent_default_model_id: "us.amazon.nova-pro-v1:0" }));
    await loadModelDefaults();
    expect(studioFallbackModelId()).toBe(STATIC_MODEL_DEFAULTS.studio_fallback_model_id);
    resetModelDefaultsForTests();
    vi.spyOn(api, "modelDefaults").mockResolvedValue(answer(CONFIGURED));
    await loadModelDefaults();
    expect(studioFallbackModelId()).toBe("us.amazon.nova-lite-v1:0");
  });

  it("an edited proposal without a model falls back to the assistant model", async () => {
    vi.spyOn(api, "modelDefaults").mockResolvedValue(answer(CONFIGURED));
    await loadModelDefaults();
    expect(proposalDraftFrom({ name: "x" }).model_id).toBe("global.moonshotai.kimi-k3");
    expect(proposalDraftFrom({ name: "x", model_id: "global.openai.gpt-6-luna" }).model_id).toBe("global.openai.gpt-6-luna");
  });
});

describe("helpers", () => {
  it("adopts the configured default only while the field still holds the seed", () => {
    expect(adoptModelDefault("seed", "seed", "configured")).toBe("configured");
    expect(adoptModelDefault("picked", "seed", "configured")).toBe("picked");
    expect(adoptModelDefault("seed", "seed", "")).toBe("seed");
  });

  it("never drops the current id from a closed list", () => {
    expect(withCurrentModel(["a", "b"], "c")).toEqual(["c", "a", "b"]);
    expect(withCurrentModel(["a", "b"], "b")).toEqual(["a", "b"]);
    expect(withCurrentModel(["a"], "  ")).toEqual(["a"]);
  });

  it("reads the model family through a geo prefix", () => {
    expect(modelFamily("global.anthropic.claude-sonnet-5")).toBe("anthropic");
    expect(modelFamily("anthropic.claude-3")).toBe("anthropic");
    expect(modelFamily("us.openai.gpt-6-sol")).toBe("openai");
    expect(modelFamily("nope")).toBe("");
  });

  it("enforces a role's family, tool use and empty rules", () => {
    const claude = { constraint: ["anthropic"], allow_empty: false, needs_tools: true };
    expect(roleAccepts(claude, "global.anthropic.claude-sonnet-5")).toBe(true);
    expect(roleAccepts(claude, "global.openai.gpt-6-sol")).toBe(false);
    expect(roleAccepts(claude, "")).toBe(false);
    const tools = { constraint: null, allow_empty: false, needs_tools: true };
    expect(roleAccepts(tools, "us.deepseek.r1-v1:0")).toBe(false);
    expect(roleAccepts({ ...tools, needs_tools: false }, "us.deepseek.r1-v1:0")).toBe(true);
    expect(roleAccepts({ constraint: null, allow_empty: true, needs_tools: false }, "")).toBe(true);
    expect(roleAccepts(tools, "bad id!")).toBe(false);
  });

  it("offers the current and default ids first, then only the catalog ids a role accepts", () => {
    const role = {
      model_id: "global.anthropic.claude-opus-5",
      default: "global.anthropic.claude-sonnet-5",
      constraint: ["anthropic"],
      allow_empty: false,
      needs_tools: true,
    };
    expect(
      roleModelOptions(role, ["global.openai.gpt-6-sol", "global.anthropic.claude-sonnet-5", "global.anthropic.claude-haiku-5-5"]),
    ).toEqual(["global.anthropic.claude-opus-5", "global.anthropic.claude-sonnet-5", "global.anthropic.claude-haiku-5-5"]);
  });
});

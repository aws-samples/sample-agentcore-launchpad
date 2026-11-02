/**
 * Platform-side model catalog shared by the creation surfaces.
 *
 * A "model source" is the hosting surface a model id belongs to. Both sources
 * ride the same AgentCore `bedrockModelConfig` union branch on the harness and
 * are distinguished by `apiFormat` alone — no API key, no bootstrap resource.
 * The backend owns that mapping (`app/deployer/harness.py`), so the frontend
 * sends `model_source` only, never `api_format`.
 */

import type { ModelCatalogEntry } from "./api";
import { activeCatalog, catalogLabel, modelFamily, supportsToolUse, toolCapableCatalog } from "./model-catalog";
import { currentModelDefaults, peekModelDefaults, STATIC_MODEL_DEFAULTS } from "./model-defaults";
import { getActiveRegion, modelAvailableInRegion } from "./regions";

export type ModelSource = "mantle" | "bedrock";

export interface ModelOption {
  model_id: string;
  label: string;
  /** Harness bedrockModelConfig.apiFormat for this model. */
  api_format: "converse_stream" | "responses" | "chat_completions";
}

/**
 * `api_format` is carried per entry rather than derived from the source so that
 * adding a `chat_completions` model later is a data change and nothing else.
 */
export const MODEL_CATALOG: Record<ModelSource, ModelOption[]> = {
  // `defaultModelFor` takes the first OFFERED entry, so ORDER PICKS THE DEFAULT
  // (for the Claude Agent SDK, the first Claude entry) — on Mantle; a native Bedrock
  // form seeds the platform's configured default instead (`defaultModelForMethod`). Terra leads
  // because Mantle's catalogue differs per region and Terra/Luna are in both
  // us-east-1 and us-west-2 while Sol is us-east-1 only. A Sol default made the
  // Harness entrance fail on a us-west-2 deployment with `404 … The model
  // 'openai.gpt-5.6-sol' does not exist`: a harness resolves Mantle in its own
  // Region and `bedrockModelConfig` has no region field, so unlike the zip path
  // it cannot be pointed at us-east-1. Sol stays selectable — it is the right
  // pick on a wholly us-east-1 deployment.
  mantle: [
    { model_id: "openai.gpt-5.6-terra", label: "GPT-5.6 Terra", api_format: "responses" },
    { model_id: "openai.gpt-5.6-luna", label: "GPT-5.6 Luna", api_format: "responses" },
    {
      model_id: "openai.gpt-5.6-sol",
      label: "GPT-5.6 Sol (us-east-1 only)",
      api_format: "responses",
    },
  ],
  // Every id here rides `converse_stream` on the harness. GLM-5.3 leads as the
  // platform default: its global inference profile answers in us-east-1,
  // us-west-2, us-east-2 and ca-central-1 (probed 2026-10-08). The global GPT-6
  // profiles follow — the harness's `responses`/`chat_completions` formats resolve
  // Mantle, where GPT-6 is bare-id and us-west-2 only, while these global
  // profiles answer Converse in every region (probed 2026-09-26).
  bedrock: [
    { model_id: "global.zai.glm-5.3", label: "GLM-5.3 (global)", api_format: "converse_stream" },
    { model_id: "zai.glm-5", label: "GLM-5", api_format: "converse_stream" },
    { model_id: "zai.glm-4.7", label: "GLM-4.7", api_format: "converse_stream" },
    { model_id: "global.openai.gpt-6-sol", label: "GPT-6 Sol (global)", api_format: "converse_stream" },
    // GPT-6.1 Sol and GLM 5.3: global profiles probed (Converse + tool use) in
    // us-west-2 and us-east-1, 2026-10-14.
    {
      model_id: "global.openai.gpt-6.1-sol",
      label: "GPT-6.1 Sol (global)",
      api_format: "converse_stream",
    },
    // GPT-6.1 Terra: Converse, tool use (auto + forced any) and reasoning.effort
    // live-probed on the global + US profiles in us-east-1 and us-west-2, 2026-11-02.
    // Converse refuses `document` blocks for it (model card) — send files as text.
    {
      model_id: "global.openai.gpt-6.1-terra",
      label: "GPT-6.1 Terra (global)",
      api_format: "converse_stream",
    },
    { model_id: "global.openai.gpt-6-astra", label: "GPT-6 Astra (global)", api_format: "converse_stream" },
    { model_id: "global.openai.gpt-6-luna", label: "GPT-6 Luna (global)", api_format: "converse_stream" },
    {
      model_id: "global.anthropic.claude-sonnet-5",
      label: "Claude Sonnet 5 (global)",
      api_format: "converse_stream",
    },
    {
      model_id: "global.anthropic.claude-sonnet-4-6",
      label: "Claude Sonnet 4.6 (global)",
      api_format: "converse_stream",
    },
    {
      model_id: "global.anthropic.claude-opus-5",
      label: "Claude Opus 5 (global)",
      api_format: "converse_stream",
    },
    { model_id: "global.moonshotai.kimi-k3", label: "Kimi K3 (global)", api_format: "converse_stream" },
    // Kimi / DeepSeek: live-probed 2026-10-12 (us-east-1 + us-west-2) for Converse,
    // tool use (auto + forced), ConverseStream with a tool. DeepSeek V3.1 is
    // us-west-2 only (`regions.ts`); DeepSeek R1 refuses toolConfig, so it is not
    // offered for agents (only to judges and prompt optimisation).
    { model_id: "us.moonshotai.kimi-k3", label: "Kimi K3 (US)", api_format: "converse_stream" },
    { model_id: "moonshotai.kimi-k2.5", label: "Kimi K2.5", api_format: "converse_stream" },
    { model_id: "moonshot.kimi-k2-thinking", label: "Kimi K2 Thinking", api_format: "converse_stream" },
    { model_id: "deepseek.v3.2", label: "DeepSeek V3.2", api_format: "converse_stream" },
    { model_id: "deepseek.v3-v1:0", label: "DeepSeek V3.1", api_format: "converse_stream" },
    {
      model_id: "global.amazon.nova-2-lite-v1:0",
      label: "Nova 2 Lite (global)",
      api_format: "converse_stream",
    },
    // OpenAI GPT-5.6 Sol through the native Bedrock US cross-region inference
    // profile (Converse) — the system architect preset's default (backend
    // `system_agents/presets.py`), kept so its configure page stays on a listed id.
    {
      model_id: "us.openai.gpt-5.6-sol",
      label: "GPT-5.6 Sol (US cross-region · Converse)",
      api_format: "converse_stream",
    },
  ],
};

/** Reasoning effort a harness may pass to the model (OpenAI GPT-5.x / GPT-6 on
 *  native Bedrock only — the backend refuses every other pairing). */
export type ReasoningEffort = "low" | "medium" | "high";
export const REASONING_EFFORTS: ReasoningEffort[] = ["low", "medium", "high"];

/** Whether `model_id` on `source` may carry a `reasoning_effort` (mirrors
 *  `AgentSpec._inference_knobs_supported`). */
export function supportsReasoningEffort(modelId: string, source: ModelSource): boolean {
  return source === "bedrock" && modelId.includes("openai.");
}

/** Form default for the methods that can express an arbitrary model. Mantle stays
 *  selectable; Bedrock leads because its global profiles work in every region. */
export const DEFAULT_MODEL_SOURCE: ModelSource = "bedrock";

/** Mirrors backend `AgentSpec.model_id`'s default (`DEFAULT_MODEL_ID`) — what a
 *  stored spec without a `model_id` means. Not the form default above. The static
 *  value; `specDefaultModelId()` is the server's configured one once loaded. */
export const SPEC_DEFAULT_MODEL_ID = STATIC_MODEL_DEFAULTS.agent_default_model_id;

/** What a stored spec without a `model_id` means: the configured
 *  `agent_default_model_id` (static {@link SPEC_DEFAULT_MODEL_ID} until loaded). */
export function specDefaultModelId(): string {
  return peekModelDefaults()?.agent_default_model_id ?? SPEC_DEFAULT_MODEL_ID;
}

/** The Claude Agent SDK can only drive Claude models, so it is pinned here. */
export const CLAUDE_SDK_MODEL_SOURCE: ModelSource = "bedrock";

/** Sentinel `<option>` value revealing the free-text model-id input. */
export const CUSTOM_MODEL_OPTION = "__custom__";

/** The first model a dropdown offers for `source` — on Bedrock the server's configured
 *  default (`agent_default_model_id`; `claude_sdk_model_id` for the Claude Agent SDK). */
export function defaultModelFor(source: ModelSource, claudeOnly = false): string {
  return modelOptionsFor(source, claudeOnly)[0].model_id;
}

/**
 * Bedrock rows in picker order: the configured default first, then the static
 * catalog, then the account's model catalog (`GET /api/model-catalog`) — deduped,
 * a known id keeping its curated label.
 */
export function mergeBedrockOptions(
  configured: string,
  staticOptions: readonly ModelOption[],
  catalog: readonly ModelCatalogEntry[],
): ModelOption[] {
  const byId = new Map<string, ModelOption>();
  for (const option of staticOptions) byId.set(option.model_id, option);
  for (const entry of catalog) {
    if (!byId.has(entry.model_id)) {
      byId.set(entry.model_id, { model_id: entry.model_id, label: catalogLabel(entry), api_format: "converse_stream" });
    }
  }
  const order = [configured, ...staticOptions.map((o) => o.model_id), ...catalog.map((e) => e.model_id)];
  const out: ModelOption[] = [];
  const seen = new Set<string>();
  for (const id of order) {
    const trimmed = id.trim();
    if (!trimmed || seen.has(trimmed)) continue;
    seen.add(trimmed);
    out.push(byId.get(trimmed) ?? { model_id: trimmed, label: trimmed, api_format: "converse_stream" });
  }
  return out;
}

/**
 * The options a dropdown may offer for `source`. `claudeOnly` narrows them to
 * Claude ids for the Claude Agent SDK, which cannot drive anything else — the
 * catalog's GLM and GPT entries would otherwise be advertised as valid there.
 * Bedrock also offers the configured default and the workspace account's catalog
 * (components re-render on its arrival through `useModelCatalog()`).
 */
export function modelOptionsFor(source: ModelSource, claudeOnly = false): ModelOption[] {
  // A geographic profile (`us.…`) only exists in its own geography, so a workspace
  // in another region is not offered it (custom entry stays possible).
  let options = MODEL_CATALOG[source].filter((option) =>
    modelAvailableInRegion(option.model_id, getActiveRegion()),
  );
  if (source === "bedrock") {
    const defaults = currentModelDefaults();
    options = mergeBedrockOptions(
      claudeOnly ? defaults.claude_sdk_model_id : defaults.agent_default_model_id,
      options,
      toolCapableCatalog(activeCatalog()),
    );
    // agents run tools: a model known to refuse toolConfig is never offered
    options = options.filter((option) => supportsToolUse(option.model_id));
  }
  return claudeOnly
    ? options.filter((option) => modelFamily(option.model_id) === "anthropic")
    : options;
}

/**
 * True when `id` is not among the options offered for `source`, i.e. it belongs
 * on the "Custom model ID…" branch. Testing against the *offered* options (not
 * the whole catalog) keeps the dropdown's displayed value and the submitted id
 * in agreement — an id from the other source, or one filtered out by
 * `claudeOnly`, is custom here even though it exists somewhere in the catalog.
 */
export function isCustomModelId(id: string, source: ModelSource, claudeOnly = false): boolean {
  return !modelOptionsFor(source, claudeOnly).some((option) => option.model_id === id);
}

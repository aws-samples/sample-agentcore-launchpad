/**
 * Platform-side model catalog shared by the creation surfaces.
 *
 * A "model source" is the hosting surface a model id belongs to. Both sources
 * ride the same AgentCore `bedrockModelConfig` union branch on the harness and
 * are distinguished by `apiFormat` alone — no API key, no bootstrap resource.
 * The backend owns that mapping (`app/deployer/harness.py`), so the frontend
 * sends `model_source` only, never `api_format`.
 */

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
  mantle: [
    { model_id: "openai.gpt-5.6-sol", label: "GPT-5.6 Sol", api_format: "responses" },
    { model_id: "openai.gpt-5.6-terra", label: "GPT-5.6 Terra", api_format: "responses" },
    { model_id: "openai.gpt-5.6-luna", label: "GPT-5.6 Luna", api_format: "responses" },
  ],
  bedrock: [
    {
      model_id: "global.anthropic.claude-sonnet-5",
      label: "Claude Sonnet 5 (global)",
      api_format: "converse_stream",
    },
    {
      model_id: "global.anthropic.claude-opus-5",
      label: "Claude Opus 5 (global)",
      api_format: "converse_stream",
    },
    {
      model_id: "global.amazon.nova-2-lite-v1:0",
      label: "Nova 2 Lite (global)",
      api_format: "converse_stream",
    },
  ],
};

/** Form default for the methods that can express an arbitrary model. */
export const DEFAULT_MODEL_SOURCE: ModelSource = "mantle";

/** The Claude Agent SDK can only drive Claude models, so it is pinned here. */
export const CLAUDE_SDK_MODEL_SOURCE: ModelSource = "bedrock";

/** Sentinel `<option>` value revealing the free-text model-id input. */
export const CUSTOM_MODEL_OPTION = "__custom__";

export function defaultModelFor(source: ModelSource): string {
  return MODEL_CATALOG[source][0].model_id;
}

/**
 * The options a dropdown may offer for `source`. `claudeOnly` narrows them to
 * Claude ids for the Claude Agent SDK, which cannot drive anything else — the
 * catalog's Nova entry would otherwise be advertised as a valid choice there.
 */
export function modelOptionsFor(source: ModelSource, claudeOnly = false): ModelOption[] {
  const options = MODEL_CATALOG[source];
  return claudeOnly
    ? options.filter((option) => option.model_id.includes("anthropic.claude"))
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

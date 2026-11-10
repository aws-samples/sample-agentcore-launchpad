/**
 * Canvas view of the platform model catalog (CN build, spec C3; N-R6-obs2).
 *
 * The canvas offers exactly the models `GET /api/capabilities` lists — the two Global
 * Bedrock cross-region profiles plus the environment's OpenAI-compatible rows (SiliconFlow
 * first), merged into `lib/models` by `applyModelCatalog` when the shell loads. Generated
 * code names the model by its Launchpad id (`BedrockModel(model_id=…)`); the Studio adapter
 * (backend `app/templates/studio_agent`) rebinds that name to the shared provider dispatch,
 * which fetches the provider's API key from AgentCore Identity at run time — so no node
 * carries a key, a region or an endpoint. DEFAULT_MODEL_ID is the fallback every
 * code-generation path uses for a node without an explicit model id; it equals the
 * backend's `DEFAULT_MODEL_ID`.
 */

import {
  type ModelOption,
  SPEC_DEFAULT_MODEL_ID,
  defaultModelFor,
  modelOption,
  modelOptionsFor,
} from '../../lib/models';

export type { ModelOption } from '../../lib/models';
export { isModelAvailable, modelOptionLabel, modelUnavailableReason } from '../../lib/models';

export const DEFAULT_MODEL_ID = SPEC_DEFAULT_MODEL_ID;

/** Sentinel value for the "Custom model ID" option in model dropdowns. */
export const CUSTOM_MODEL_OPTION = '__custom__';

/**
 * The models a canvas node may run on: every catalog row that supports tool calling
 * (`lib/models.modelOptionsFor`), in catalog order — Bedrock rows first, then the
 * environment's OpenAI-compatible rows. A row whose provider is not configured stays
 * listed with `available: false`; the picker renders it disabled (`models.unavailable`).
 * Read at render time: the catalog is merged in place after the shell loads capabilities.
 */
export function studioModelOptions(): ModelOption[] {
  return modelOptionsFor('bedrock');
}

/** The model a newly dropped agent node (or a switch to the catalog provider) starts on:
 *  the first AVAILABLE catalog row — the platform default, never an unconfigured one. */
export function defaultNewAgentModel(): ModelOption {
  const id = defaultModelFor('bedrock');
  return modelOption(id) ?? studioModelOptions()[0];
}

/**
 * Display label for a catalog model id: the catalog's label when `modelId` is
 * listed, else the raw id, else (no id at all) the default new-agent model's
 * label. Never a hardcoded brand/model string (fix round 1, C5) — a node that
 * carries no explicit `modelName` must still show a label that tracks its
 * actual `modelId`, not a stale name from a deleted catalog entry.
 */
export function catalogModelLabel(modelId: string | null | undefined): string {
  if (!modelId) return defaultNewAgentModel().label;
  return modelOption(modelId)?.label ?? modelId;
}

/**
 * Anthropic-only request shapes (adaptive thinking / output_config). GLM 5.3 and Kimi K3
 * reject unknown additionalModelRequestFields, so the emitters gate that block on this
 * and a GLM/Kimi node with the thinking toggle still generates a plain model (R17).
 */
export function isAnthropicModel(modelId: string | null | undefined): boolean {
  return /(^|\.)anthropic\./.test(modelId || '');
}

/**
 * OpenAI GPT-5.x / GPT-6 on native Bedrock (any profile prefix). These take a
 * reasoning effort instead of Anthropic's adaptive thinking, and no Anthropic
 * cache points. gpt-oss is deliberately excluded — its request shape is not verified.
 */
export function isBedrockOpenAiGpt(modelId: string | null | undefined): boolean {
  return /(^|\.)openai\.gpt-\d/.test(modelId || '');
}

/** GPT reasoning effort scale on Bedrock; Anthropic-only tiers clamp to high. */
export const GPT_REASONING_EFFORTS = ['low', 'medium', 'high'] as const;

function gptEffort(effort: string | null | undefined): string | null {
  if (!effort) return null;
  if (effort === 'minimal') return 'low';
  return (GPT_REASONING_EFFORTS as readonly string[]).includes(effort) ? effort : 'high';
}

/**
 * `BedrockModel(...)` for an OpenAI GPT model. The effort rides Converse's
 * `additionalModelRequestFields` as `{"reasoning": {"effort": …}}` — the shape the
 * managed harness sends for the same models (backend `app/deployer/harness.py`).
 * Anthropic's adaptive-thinking block and cache kwargs are never emitted here.
 * `indent` is the leading indentation of the call's closing paren; argument
 * lines get four more (the two emitter scopes differ only in that).
 */
export function gptBedrockModelConfig(
  varName: string,
  modelId: string,
  maxTokens: number,
  thinkingEnabled: boolean | undefined,
  reasoningEffort: string | undefined,
  indent: string,
): string {
  const inner = `${indent}    `;
  let code = `${varName}_model = BedrockModel(
${inner}model_id="${modelId}",
${inner}max_tokens=${maxTokens}`;
  // the panel shows Medium until a tier is picked, so an unset effort means medium
  const effort = thinkingEnabled ? gptEffort(reasoningEffort || 'medium') : null;
  if (effort) {
    code += `,
${inner}additional_request_fields={
${inner}    "reasoning": {
${inner}        "effort": "${effort}"
${inner}    }
${inner}}`;
  }
  return `${code}\n${indent})`;
}

/** Display name marking a node as using a user-entered (custom) model id. */
export const CUSTOM_MODEL_NAME = 'Custom model';

/**
 * True when the node should show the custom-model-id input: either the stored
 * id is not in the catalog (e.g. a legacy/removed id), or the node was
 * explicitly switched to "Custom model ID…" (marked via modelName) and the id
 * is still being typed.
 */
export function isCustomModel(
  modelId: string | undefined | null,
  modelName?: string | null,
): boolean {
  if (modelName === CUSTOM_MODEL_NAME) return true;
  if (!modelId) return false;
  return modelOption(modelId) === undefined;
}

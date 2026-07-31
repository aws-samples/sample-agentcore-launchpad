/**
 * Shared Bedrock model catalog and default model id.
 *
 * DEFAULT_MODEL_ID is the single source of truth for the fallback model used by
 * every code-generation fallback path — nodes that carry no explicit model id
 * (orchestrator/swarm drops, legacy flows). It is deliberately NOT what a newly
 * dropped agent node gets: that is DEFAULT_MANTLE_MODEL_ID (see FlowEditor).
 * Changing these fallbacks would retroactively change existing flows.
 *
 * Claude Sonnet 5 / 4.6 / Opus 4.8 ids verified against
 * `aws bedrock list-inference-profiles` (us-west-2) on 2026-07-10.
 * xai.grok-4.3 / openai.gpt-5.5 / openai.gpt-5.4 are available in
 * us-east-1 (per user confirmation; not visible in this account's
 * listings — likely requires model access enablement).
 */

import { MODEL_CATALOG } from '../../lib/models';

export const DEFAULT_MODEL_ID = 'global.anthropic.claude-sonnet-5';

/** Sentinel value for the "Custom model ID" option in model dropdowns. */
export const CUSTOM_MODEL_OPTION = '__custom__';

export interface BedrockModelOption {
  model_id: string;
  model_name: string;
}

export const BEDROCK_MODELS: BedrockModelOption[] = [
  {
    model_id: 'global.anthropic.claude-sonnet-5',
    model_name: 'Claude Sonnet 5 (global)',
  },
  {
    model_id: 'us.anthropic.claude-sonnet-5',
    model_name: 'Claude Sonnet 5 (US)',
  },
  {
    model_id: 'global.anthropic.claude-sonnet-4-6',
    model_name: 'Claude Sonnet 4.6 (global)',
  },
  {
    model_id: 'us.anthropic.claude-sonnet-4-6',
    model_name: 'Claude Sonnet 4.6 (US)',
  },
  {
    model_id: 'eu.anthropic.claude-sonnet-4-6',
    model_name: 'Claude Sonnet 4.6 (EU)',
  },
  {
    model_id: 'global.anthropic.claude-opus-4-8',
    model_name: 'Claude Opus 4.8 (global)',
  },
  {
    model_id: 'us.anthropic.claude-opus-4-8',
    model_name: 'Claude Opus 4.8 (US)',
  },
  {
    model_id: 'eu.anthropic.claude-opus-4-8',
    model_name: 'Claude Opus 4.8 (EU)',
  },
  {
    model_id: 'openai.gpt-oss-120b-1:0',
    model_name: 'GPT-OSS-120B',
  },
  {
    model_id: 'qwen.qwen3-235b-a22b-2507-v1:0',
    model_name: 'Qwen3 235B A22B 2507',
  },
  {
    model_id: 'qwen.qwen3-32b-v1:0',
    model_name: 'Qwen3 32B (dense)',
  },
  {
    model_id: 'qwen.qwen3-coder-480b-a35b-v1:0',
    model_name: 'Qwen3 Coder 480B A35B Instruct',
  },
  {
    model_id: 'deepseek.v3-v1:0',
    model_name: 'DeepSeek-V3.1',
  },
  {
    model_id: 'us.amazon.nova-premier-v1:0',
    model_name: 'Amazon Nova Premier v1',
  },
  {
    model_id: 'us.amazon.nova-pro-v1:0',
    model_name: 'Amazon Nova Pro v1',
  },
];

/**
 * Amazon Bedrock Mantle provider — OpenAI-compatible endpoint served via
 * `OpenAIResponsesModel` (OpenAI Responses API). Grok / GPT models are only
 * reachable through Mantle, not the native BedrockModel InvokeModel path.
 *
 * Auth is IAM by default: `bedrock_mantle_config` makes the Strands SDK mint a
 * short-lived bearer token from the ambient credential chain (the runtime
 * execution role) on every request, so no key is provisioned. A node may still
 * carry an explicit `BEDROCK_API_KEY`, which switches codegen to the keyed
 * `client_args` form — the two are mutually exclusive in the SDK.
 */
export const MANTLE_PROVIDER = 'Amazon Bedrock (Mantle)';

/**
 * Default region for the Mantle endpoint. us-east-1 carries the widest
 * catalogue — every id below exists there, whereas us-west-2 is missing
 * `openai.gpt-5.6-sol` and `openai.gpt-5.5`. Enumerate a region's real
 * catalogue with `GET https://bedrock-mantle.<region>.api.aws/v1/models`
 * (bearer token from `aws_bedrock_token_generator`); Mantle models never appear
 * in `bedrock:ListFoundationModels`.
 */
export const DEFAULT_MANTLE_REGION = 'us-east-1';

/**
 * Build the Mantle OpenAI-compatible base URL for a region and model.
 *
 * Only `openai.gpt-5.*` ids are served from `/openai/v1`; every other
 * Mantle-routed model (grok, gpt-oss, …) lives under `/v1`. Mirrors
 * `strands/models/_openai_bedrock.py:28-39`, which the SDK applies itself on the
 * IAM path — so this matters only for the explicit-key `client_args` form.
 */
export function mantleBaseUrl(region: string, modelId?: string | null): string {
  const path = modelId?.startsWith('openai.gpt-5.') ? '/openai/v1' : '/v1';
  return `https://bedrock-mantle.${region || DEFAULT_MANTLE_REGION}.api.aws${path}`;
}

/**
 * Mantle ids offered by the canvas. The GPT-5.6 family comes from the shared
 * platform catalog so the wizard and the canvas cannot drift; the older ids stay
 * listed so flows published against them do not fall into `isCustomMantleModel`.
 */
export const MANTLE_MODELS: BedrockModelOption[] = [
  ...MODEL_CATALOG.mantle.map((m) => ({ model_id: m.model_id, model_name: m.label })),
  { model_id: 'openai.gpt-5.5', model_name: 'GPT-5.5 (OpenAI, us-east-1 only)' },
  { model_id: 'openai.gpt-5.4', model_name: 'GPT-5.4 (OpenAI)' },
  { model_id: 'xai.grok-4.3', model_name: 'Grok 4.3 (xAI)' },
];

export const DEFAULT_MANTLE_MODEL_ID = MANTLE_MODELS[0].model_id;

/**
 * The `OpenAIResponsesModel` auth argument for a Mantle node, as Python source.
 *
 * Two mutually exclusive forms — the SDK raises if `client_args` carries
 * `api_key`/`base_url` alongside `bedrock_mantle_config`:
 *
 * - **no key (the default)** → `bedrock_mantle_config`. The SDK mints a
 *   short-lived bearer token from the ambient AWS credential chain (the runtime
 *   execution role) on every request and derives the base URL itself.
 * - **explicit key** → today's `client_args` form, so flows published with a
 *   `BEDROCK_API_KEY` keep generating exactly the code they generated before.
 *
 * `indent` is the leading indentation of the emitted argument line; inner lines
 * get four more. Shared by all three emitters (agent scope, agent-as-tool scope,
 * and the graph generator) so the two forms cannot drift apart.
 */
export function mantleModelArgs(
  opts: {
    apiKey?: string | null;
    region?: string | null;
    modelId?: string | null;
  },
  indent: string,
): string {
  const region = opts.region || DEFAULT_MANTLE_REGION;
  if (!opts.apiKey) {
    return `\n${indent}bedrock_mantle_config={"region": "${region}"},`;
  }
  const inner = `${indent}    `;
  const clientArgs = [
    `"api_key": os.environ.get("BEDROCK_API_KEY")`,
    `"base_url": "${mantleBaseUrl(region, opts.modelId)}"`,
  ];
  return `\n${indent}client_args={\n${inner}${clientArgs.join(`,\n${inner}`)}\n${indent}},`;
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
  return !BEDROCK_MODELS.some((m) => m.model_id === modelId);
}

/** Same as {@link isCustomModel} but against the Mantle catalog. */
export function isCustomMantleModel(
  modelId: string | undefined | null,
  modelName?: string | null,
): boolean {
  if (modelName === CUSTOM_MODEL_NAME) return true;
  if (!modelId) return false;
  return !MANTLE_MODELS.some((m) => m.model_id === modelId);
}

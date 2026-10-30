/**
 * Platform-side model catalog shared by the creation surfaces (CN build, spec C3).
 *
 * Two Global Bedrock cross-region profiles are always present (entered from `model_region`,
 * us-west-2, with the workspace's Global Bedrock API key held by the AgentCore Identity
 * provider `model_credential`). Since N-R6 the backend catalog also carries the chat models
 * of OpenAI-compatible providers (SiliconFlow first) — a per-environment Settings override,
 * so those rows are NOT hard-coded here: `GET /api/capabilities` returns the whole catalog
 * with its provider dimension and `applyModelCatalog` merges it into the lists below as soon
 * as the shell loads capabilities. The backend refuses every other id (`model.not_allowed`),
 * so the "Custom model ID…" branch only ever shows a stored legacy id — it cannot publish.
 * Mirrors backend `app/schemas/agent.py` (MODEL_CATALOG / PROVIDERS / DEFAULT_MODEL_ID).
 */

import { currentModelDefaults } from "./model-defaults";

export type ModelSource = "bedrock";
/** Which kind of endpoint serves a model (backend `PROVIDERS[*].kind`). */
export type ModelProviderKind = "bedrock" | "openai_compatible";
/** D21 additive: where a catalog row's capabilities came from (`builtin` seed, a `probed`
 *  endpoint, or an administrator's `manual` override in Settings → Model Providers). */
export type CatalogModelSource = "builtin" | "probed" | "manual";
/** D21 additive: `models[]` of `/api/capabilities` lists chat rows only (pickers are
 *  chat-only); the active embedding row is exposed separately. */
export type CatalogModelKind = "chat" | "embedding";

export interface ModelOption {
  model_id: string;
  label: string;
  /** Bedrock models answer Converse (streamed); OpenAI-compatible ones chat completions. */
  api_format: "converse_stream" | "chat_completions";
  /** backend `PROVIDERS` key — `bedrock`, `siliconflow`, … */
  provider: string;
  provider_kind: ModelProviderKind;
  provider_label: string;
  /** function calling — `false` ⇒ judge-only, never offered as an agent model */
  tools: boolean;
  /** the provider's API key is configured in this workspace (N-R6-obs1) — `false` rows
   *  stay listed but every picker renders them disabled with the SSM parameter to create */
  available: boolean;
  /** D21 additive (absent on an older server) */
  source?: CatalogModelSource;
  kind?: CatalogModelKind;
}

/** One row of `GET /api/capabilities` → `models` (CN additive, N-R6). */
export interface CatalogModel {
  model_id: string;
  label: string;
  provider: string;
  provider_kind: ModelProviderKind;
  provider_label: string;
  tools: boolean;
  default?: boolean;
  /** CN additive (N-R6-obs1); absent on an older server = available */
  available?: boolean;
  /** D21 additive: capability provenance / row kind; absent on an older server */
  source?: CatalogModelSource;
  kind?: CatalogModelKind;
  /** D21 additive: the registry row is enabled (always true in this list) */
  enabled?: boolean;
}

/** One entry of `GET /api/capabilities` → `model_providers` (CN additive, N-R6 / N-R6-obs1). */
export interface CatalogProvider {
  kind: ModelProviderKind;
  label: string;
  credential_provider?: string;
  /** the workspace holds this provider's key (bootstrap record, else hub env) */
  configured?: boolean;
  /** the SSM SecureString the operator stores the key in */
  ssm_parameter?: string;
  api_key_env?: string;
  /** D21 additive: registry row id / enabled flag / last probe state */
  id?: string;
  enabled?: boolean;
  probe_state?: "never" | "ok" | "failed";
}

export interface ModelProviderInfo {
  kind: ModelProviderKind;
  label: string;
  configured: boolean;
  ssm_parameter: string;
}

const BEDROCK_PROVIDER: ModelProviderInfo = {
  kind: "bedrock", label: "Global Bedrock", configured: true,
  ssm_parameter: "launchpad-global-bedrock-api-key",
};

/** `PROVIDERS` as the server reports them; Bedrock (required, always configured) is the one
 *  static row. Mutable in place like `MODEL_CATALOG` (`applyModelCatalog`). */
export const MODEL_PROVIDERS: Record<string, ModelProviderInfo> = { bedrock: { ...BEDROCK_PROVIDER } };

const BEDROCK_DEFAULTS: ModelOption[] = [
  { model_id: "global.zai.glm-5.3", label: "GLM 5.3 (Z.AI · global)", api_format: "converse_stream",
    provider: "bedrock", provider_kind: "bedrock", provider_label: "Global Bedrock", tools: true, available: true },
  { model_id: "global.moonshotai.kimi-k3", label: "Kimi K3 (Moonshot AI · global)", api_format: "converse_stream",
    provider: "bedrock", provider_kind: "bedrock", provider_label: "Global Bedrock", tools: true, available: true },
];

/** Mutable on purpose: `applyModelCatalog` appends the server's extra rows in place so every
 *  module that imported the array (pickers, judge/actor option lists) sees them. */
export const MODEL_CATALOG: Record<ModelSource, ModelOption[]> = {
  bedrock: [...BEDROCK_DEFAULTS],
};

export const ALLOWED_MODEL_IDS: string[] = MODEL_CATALOG.bedrock.map((option) => option.model_id);

/** Merge the backend catalog (every provider) into the static lists — idempotent; rows the
 *  server no longer lists are kept (a stored spec may still show them). `providers`
 *  (N-R6-obs1) carries the configured flag + SSM parameter the unavailable tooltip names. */
export function applyModelCatalog(
  rows: CatalogModel[] | undefined | null,
  providers?: Record<string, CatalogProvider> | null,
): void {
  for (const [name, p] of Object.entries(providers ?? {})) {
    if (!name || !p) continue;
    MODEL_PROVIDERS[name] = {
      kind: p.kind || "bedrock",
      label: p.label || name,
      // a required provider (Bedrock) is always configured; an older server without the
      // flag reports nothing — treat as configured (the backend refuses at create anyway)
      configured: p.kind === "bedrock" ? true : p.configured !== false,
      ssm_parameter: p.ssm_parameter || `launchpad-${name}-api-key`,
    };
  }
  for (const row of rows ?? []) {
    if (!row?.model_id) continue;
    const existing = MODEL_CATALOG.bedrock.find((o) => o.model_id === row.model_id);
    const option: ModelOption = {
      model_id: row.model_id,
      label: row.label || row.model_id,
      api_format: row.provider_kind === "openai_compatible" ? "chat_completions" : "converse_stream",
      provider: row.provider || "bedrock",
      provider_kind: row.provider_kind || "bedrock",
      provider_label: row.provider_label || row.provider || "bedrock",
      tools: row.tools !== false,
      available: row.available !== false,
    };
    if (row.source) option.source = row.source;
    if (row.kind) option.kind = row.kind;
    if (existing) Object.assign(existing, option);
    else MODEL_CATALOG.bedrock.push(option);
    if (!ALLOWED_MODEL_IDS.includes(row.model_id)) ALLOWED_MODEL_IDS.push(row.model_id);
  }
}

/** test-only: back to the two static Bedrock rows */
export function _resetModelCatalog(): void {
  MODEL_CATALOG.bedrock.splice(0, MODEL_CATALOG.bedrock.length, ...BEDROCK_DEFAULTS.map((o) => ({ ...o })));
  ALLOWED_MODEL_IDS.splice(0, ALLOWED_MODEL_IDS.length, ...BEDROCK_DEFAULTS.map((o) => o.model_id));
  for (const key of Object.keys(MODEL_PROVIDERS)) delete MODEL_PROVIDERS[key];
  MODEL_PROVIDERS.bedrock = { ...BEDROCK_PROVIDER };
}

/** The catalog row for `id`, if any (a stored legacy id has none). */
export function modelOption(id: string): ModelOption | undefined {
  return MODEL_CATALOG.bedrock.find((o) => o.model_id === id);
}

/** `false` only for a catalog row whose provider has no key in this workspace; a custom /
 *  legacy id answers `true` (the backend's catalog check is what refuses it). */
export function isModelAvailable(id: string): boolean {
  return modelOption(id)?.available !== false;
}

/** Why `id` cannot be picked — the i18n params of `models.unavailable` ("<provider> not
 *  configured — store the key as SSM parameter <name>"); `null` when it can. */
export function modelUnavailableReason(id: string): { provider: string; parameter: string } | null {
  const option = modelOption(id);
  if (!option || option.available) return null;
  const provider = MODEL_PROVIDERS[option.provider];
  return {
    provider: provider?.label ?? option.provider_label,
    parameter: provider?.ssm_parameter ?? `launchpad-${option.provider}-api-key`,
  };
}

/** `<option>` / listbox label of a catalog row: the label, plus a short "not configured"
 *  suffix for an unavailable one (`suffix` = the translated `models.unavailableShort`). */
export function modelOptionLabel(option: ModelOption, suffix: string, withId = false): string {
  const base = withId ? `${option.label} · ${option.model_id}` : option.label;
  return option.available ? base : `${base} · ${suffix}`;
}

/** `models.provider.<kind>` i18n key for the badge the pickers show next to a model. */
export function providerBadgeKey(id: string): string {
  return `models.provider.${modelOption(id)?.provider_kind ?? "bedrock"}`;
}

/** Reasoning effort a harness may pass to the model (OpenAI GPT ids on native Bedrock
 *  only — none is in the CN catalog, so the control stays hidden for every listed model). */
export type ReasoningEffort = "low" | "medium" | "high";
export const REASONING_EFFORTS: ReasoningEffort[] = ["low", "medium", "high"];

/** Mirrors `AgentSpec._inference_knobs_supported`. */
export function supportsReasoningEffort(modelId: string, source: ModelSource): boolean {
  return source === "bedrock" && modelId.includes("openai.");
}

export const DEFAULT_MODEL_SOURCE: ModelSource = "bedrock";

/** Mirrors backend `AgentSpec.model_id` / `model_region` / `model_credential` defaults. */
export const SPEC_DEFAULT_MODEL_ID = "global.zai.glm-5.3";
export const SPEC_DEFAULT_MODEL_REGION = "us-west-2"; // region-boundary: global-bedrock
export const SPEC_DEFAULT_MODEL_CREDENTIAL = "launchpad-global-bedrock";

/** Sentinel `<option>` value revealing the free-text model-id input. */
export const CUSTOM_MODEL_OPTION = "__custom__";

/** The model a dropdown seeds for `source`: the platform's configured `agent_default` role
 *  (`GET /api/model-defaults`, r15-e) when it is an offered, available option — else the first
 *  AVAILABLE model (the built-in default, Global Bedrock GLM 5.3 — never a row whose
 *  provider is not configured). */
export function defaultModelFor(source: ModelSource): string {
  const options = modelOptionsFor(source);
  const configured = currentModelDefaults().agent_default;
  const match = options.find((o) => o.model_id === configured && o.available);
  return (match ?? options.find((o) => o.available) ?? options[0]).model_id;
}

/** Models an AGENT may run on: every catalog row that supports tool calling (an
 *  OpenAI-compatible row without tools is judge-only — `judgeModelOptions`). */
export function modelOptionsFor(source: ModelSource): ModelOption[] {
  return MODEL_CATALOG[source].filter((o) => o.tools);
}

/** Models an evaluation judge / actor may run on: the whole catalog. */
export function judgeModelOptions(): ModelOption[] {
  return MODEL_CATALOG.bedrock;
}

/** True when `id` is not among the offered options, i.e. it belongs on the
 *  "Custom model ID…" branch (a stored pre-CN id, for display only). */
export function isCustomModelId(id: string, source: ModelSource): boolean {
  return !MODEL_CATALOG[source].some((option) => option.model_id === id);
}

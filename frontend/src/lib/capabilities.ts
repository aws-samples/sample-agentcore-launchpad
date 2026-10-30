import { useEffect, useState } from "react";

import { applyModelCatalog, type CatalogModel, type CatalogProvider } from "./models";
import { featureHidden } from "../v2/hiddenSurfaces";
import { setDisplayTimeZone } from "./timestamps";

export type Feature =
  | "agents" | "chat" | "registry" | "memory" | "knowledge_bases"
  | "identity" | "gateway" | "observability" | "workspaces" | "users" | "videos"
  | "evaluation" | "optimization" | "skill_lab" | "policy";

export const ALL_FEATURES: Feature[] = [
  "agents", "chat", "registry", "memory", "knowledge_bases",
  "identity", "gateway", "observability", "workspaces", "users", "videos",
  "evaluation", "optimization", "skill_lab", "policy",
];

/** `GET /api/capabilities.policy` (Global model): how many policy engines the workspace has
 *  and how many gateways carry an attachment. */
export interface PolicyCapability {
  engines: number;
  attachedGateways: number;
}

/**
 * `GET /api/capabilities.methods[<method>]` (round 16, N15-obs16): the creation-time
 * option domain each deploy method's generate stage can render — declared once on the
 * backend (`deployer.pipeline.MethodCapabilities`), refused there with 422
 * `agent.method_unsupported`, and read here so the wizard disables (never hides) what the
 * current method cannot take.
 */
export interface MethodCapability {
  builtin_tools: string[];
  tool_types: string[];
  tool_auth_modes: string[];
  native_tools: string[];
  protocols: string[];
  memory: boolean;
  knowledge_bases: boolean;
  byoc_kinds: string[];
  requires: string[];
}
export type MethodCapabilities = Record<string, MethodCapability>;

/**
 * `GET /api/capabilities` — the enabled modules plus the evaluation opt-ins the forms
 * need before the user picks an option (CN additive, N12-obs41).
 */
export interface Capabilities {
  features: Set<Feature>;
  /** per deploy method, what its generate stage renders; null = unknown (older server or
   *  unreachable) → the wizard offers everything and the server's 422 decides */
  methods: MethodCapabilities | null;
  /** `Settings.eval_code_evaluators_enabled` — the Lambda "Code" evaluator definition
   *  is offered only when the workspace opted in; the backend refuses it otherwise
   *  (403 `evaluation.code_evaluators_disabled`). */
  codeEvaluatorsEnabled: boolean;
  /** null when the server has no `policy` feature (or an older server) */
  policy: PolicyCapability | null;
  /** `GET /api/capabilities.tools.browser_signing` (N17-obs258): Web Bot Auth browser
   *  signing exists in the workspace's partition — false in aws-cn, so the Browser demo
   *  hides the toggle (the backend refuses it there: 409
   *  `browser.signing_unsupported_in_partition`). Unconfirmed = false. */
  browserSigning: boolean;
}

interface CapabilitiesBody {
  features: string[];
  evaluation?: { code_evaluators_enabled?: boolean };
  /** CN additive (N17-obs258): partition-scoped built-in tool options */
  tools?: { browser_signing?: boolean };
  /** additive: present only when the `policy` feature is on */
  policy?: { engines?: number; attached_gateways?: number };
  /** CN additive (N-R6): the model catalog with its provider dimension — merged into
   *  `lib/models` so the pickers offer the environment's OpenAI-compatible rows too. */
  models?: CatalogModel[];
  /** CN additive (N-R6-obs1): per provider `configured` + `ssm_parameter`, so an
   *  unavailable row's tooltip can name the parameter the operator must store. */
  model_providers?: Record<string, CatalogProvider>;
  /** D21 additive: the registry snapshot version the catalog above was read from */
  catalog_version?: number;
  /** D21 additive: the active embedding row (not in `models[]` — pickers are chat-only) */
  embedding?: { model_id: string; dimension: number; provider: string } | null;
  /** N17-obs463: the IANA zone the API's times are in and the console formats in */
  display_timezone?: string;
  /** round 16 additive: the creation-time option domain per deploy method */
  methods?: Record<string, Partial<MethodCapability>>;
}

const LIST_FIELDS = ["builtin_tools", "tool_types", "tool_auth_modes", "native_tools", "protocols", "byoc_kinds", "requires"] as const;

/** Normalise one method's block: every list present (missing → empty), flags boolean. */
export function parseMethodCapability(raw: Partial<MethodCapability> | undefined): MethodCapability {
  const out = { memory: raw?.memory !== false, knowledge_bases: raw?.knowledge_bases !== false } as MethodCapability;
  for (const key of LIST_FIELDS) {
    const value = raw?.[key];
    out[key] = Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
  }
  return out;
}

/** Parse the server payload; unknown features are dropped, a missing `evaluation`
 *  block (older server) leaves the opt-ins off. */
export function parseCapabilities(body: CapabilitiesBody): Capabilities {
  setDisplayTimeZone(body.display_timezone); // N17-obs463 (missing: keep Asia/Shanghai)
  if (Array.isArray(body.models) || body.model_providers) {
    applyModelCatalog(Array.isArray(body.models) ? body.models : [], body.model_providers ?? null);
  }
  const features = new Set(
    body.features.filter(
      (f): f is Feature => (ALL_FEATURES as string[]).includes(f) && !featureHidden(f as Feature),
    ),
  );
  const policy: PolicyCapability | null =
    features.has("policy") && body.policy
      ? { engines: Number(body.policy.engines ?? 0), attachedGateways: Number(body.policy.attached_gateways ?? 0) }
      : null;
  const methods: MethodCapabilities | null =
    body.methods && typeof body.methods === "object"
      ? Object.fromEntries(Object.entries(body.methods).map(([name, raw]) => [name, parseMethodCapability(raw)]))
      : null;
  return {
    features,
    methods,
    codeEvaluatorsEnabled: body.evaluation?.code_evaluators_enabled === true,
    policy,
    browserSigning: body.tools?.browser_signing === true,
  };
}

let cached: Capabilities | null = null;
let inflight: Promise<Capabilities> | null = null;

export async function loadCapabilities(fetcher: typeof fetch = fetch): Promise<Capabilities> {
  if (cached) return cached;
  if (!inflight) {
    inflight = fetcher("/api/capabilities", { cache: "no-store" })
      .then(async (res) => {
        if (!res.ok) throw new Error(`capabilities ${res.status}`);
        cached = parseCapabilities((await res.json()) as CapabilitiesBody);
        return cached;
      })
      .catch(() => {
        // backend unreachable: show every module rather than nothing (the topbar
        // health chip reports the outage) — but never an opt-in the server did not
        // confirm, the backend would refuse it anyway
        cached = { features: new Set(ALL_FEATURES.filter((f) => !featureHidden(f))), methods: null, codeEvaluatorsEnabled: false, policy: null, browserSigning: false };
        return cached;
      })
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
}

export async function loadFeatures(fetcher: typeof fetch = fetch): Promise<Set<Feature>> {
  return (await loadCapabilities(fetcher)).features;
}

/** test-only */
export function _resetFeaturesCache(): void {
  cached = null;
  inflight = null;
}

/** Drop the cached capabilities so the next `loadCapabilities()` re-reads the model
 *  catalog — the Settings → Model Providers page calls it after every registry write
 *  (plan §5.5: the pickers pick a change up on their next capabilities load). */
export function invalidateCapabilities(): void {
  cached = null;
}

export function useCapabilities(): { capabilities: Capabilities | null; loading: boolean } {
  const [capabilities, setCapabilities] = useState<Capabilities | null>(cached);
  useEffect(() => {
    let live = true;
    void loadCapabilities().then((c) => live && setCapabilities(c));
    return () => {
      live = false;
    };
  }, []);
  return { capabilities, loading: capabilities === null };
}

export function useFeatures(): { features: Set<Feature> | null; loading: boolean } {
  const { capabilities, loading } = useCapabilities();
  return { features: capabilities?.features ?? null, loading };
}

/** `false` while loading — an option the server has not confirmed is never offered. */
export function useCodeEvaluatorsEnabled(): boolean {
  return useCapabilities().capabilities?.codeEvaluatorsEnabled === true;
}

/** `false` while loading or unconfirmed — Web Bot Auth is offered only where the server
 *  says the partition signs (N17-obs258: never in aws-cn). */
export function useBrowserSigningSupported(): boolean {
  return useCapabilities().capabilities?.browserSigning === true;
}

/** The workspace's policy counts; null while loading or without the feature. */
export function usePolicyCapability(): PolicyCapability | null {
  return useCapabilities().capabilities?.policy ?? null;
}

/** Per-method creation-time domain; null while loading or from an older server. */
export function useMethodCapabilities(): MethodCapabilities | null {
  return useCapabilities().capabilities?.methods ?? null;
}

export function hasFeature(features: Set<Feature> | null, feature: Feature): boolean {
  return features === null ? true : features.has(feature);
}

/** First path segment → feature; `/v2/...` is unwrapped; unknown → null (always routable). */
const PATH_FEATURE: Record<string, Feature> = {
  agents: "agents", create: "agents", chat: "chat", registry: "registry", memory: "memory",
  "knowledge-bases": "knowledge_bases", connections: "identity",
  governance: "gateway", policy: "policy", observability: "observability", workspaces: "workspaces", users: "users",
  eval: "evaluation", evaluation: "evaluation",
  "skill-lab": "skill_lab",
  // hub-global video library (Learn → Videos) and its admin page
  videos: "videos", "video-management": "videos",
};

export function featureForPath(pathname: string): Feature | null {
  const parts = pathname.split("/").filter(Boolean);
  const head = parts[0] === "v2" ? parts[1] : parts[0];
  return head ? PATH_FEATURE[head] ?? null : null;
}

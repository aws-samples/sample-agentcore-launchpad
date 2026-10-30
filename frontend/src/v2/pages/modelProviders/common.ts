import {
  ApiError,
  type ModelCapabilitySource,
  type ModelProviderKind,
  type ModelProviderOut,
  type ModelRowKind,
  type ModelRowOut,
} from "../../../lib/api";
import type { TagTone } from "../../ui";
import type { Feature } from "../../../lib/capabilities";
import { type CreateEntry, entryShown, featureHidden } from "../../hiddenSurfaces";

/** `?view=` sub-surfaces of the page (repo convention: query param, not nested routes). */
export type View = "providers" | "roles" | "workspaces" | "changes";
export const VIEWS: View[] = ["providers", "roles", "workspaces", "changes"];
/** N17-obs203: a member reaches the providers view only — the platform roles, the per-workspace
 *  flag matrix and the journal are administrators' surfaces (the backend answers 403). */
// N17-obs251 (user ruling 2026-10-22): members see the Model roles tab read-only
export const MEMBER_VIEWS: View[] = ["providers", "roles"];

export function isView(value: string | null): value is View {
  return value !== null && (VIEWS as string[]).includes(value);
}

/** The `data-testid`-free scope badge tone: personal = outline, workspace = blue. */
export function scopeTone(scope: ModelProviderOut["scope"]): TagTone {
  return scope === "workspace" ? "blue" : "outline";
}

export const PROVIDER_KINDS: ModelProviderKind[] = ["bedrock", "openai_compatible"];
export const MODEL_KINDS: ModelRowKind[] = ["chat", "embedding"];

/** Mirrors the backend validators (plan §2.1 / §1.1 naming invariants). */
export const PROVIDER_NAME_RE = /^[a-z][a-z0-9-]{1,31}$/;
export const SSM_PARAMETER_RE = /^launchpad-[a-z0-9-]+-api-key$/;

export function defaultSsmParameter(name: string): string {
  return `launchpad-${name || "<name>"}-api-key`;
}

export function defaultCredentialProvider(name: string): string {
  return `launchpad-${name || "<name>"}`;
}

export function defaultIdPrefix(name: string, kind: ModelProviderKind): string {
  return kind === "bedrock" ? "" : `${name || "<name>"}.`;
}

/** `https://` + a hostname (no IP literal): the client-side half of the §9.3 egress guard.
 *  N17-obs203: there is no operator allowlist precondition any more — the server resolves the
 *  host before every request and refuses private / loopback / link-local / internal names; an
 *  operator lock-mode allowlist or denylist (config/launchpad.yaml) is a server setting the page
 *  cannot see and surfaces as 422 `model_provider.host_not_allowed`. */
export function baseUrlProblem(raw: string): "required" | "https" | "host" | null {
  const value = raw.trim();
  if (!value) return "required";
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return "https";
  }
  if (url.protocol !== "https:") return "https";
  const host = url.hostname;
  if (!host || /^\d{1,3}(\.\d{1,3}){3}$/.test(host) || host.startsWith("[") || host === "localhost") return "host";
  return null;
}

export function endpointHost(url: string | null | undefined): string {
  if (!url) return "—";
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

export const SOURCE_TONE: Record<ModelCapabilitySource, TagTone> = {
  builtin: "gray",
  probed: "green",
  manual: "blue",
  unknown: "orange",
};

/** The capability chips of a model row, in display order. `key` is also the
 *  `capability_sources` key (`temperature` / `top_p` live under `inference_params`). */
export const CAPABILITY_KEYS = [
  "tools",
  "streaming",
  "vision",
  "temperature",
  "top_p",
  "default_max_tokens",
  "max_output_tokens",
  "default_judge_max_tokens",
  "embedding_dimension",
] as const;
export type CapabilityKey = (typeof CAPABILITY_KEYS)[number];

export function capabilityValue(model: ModelRowOut, key: CapabilityKey): boolean | number | null {
  switch (key) {
    case "temperature":
      return model.inference_params?.temperature ?? null;
    case "top_p":
      return model.inference_params?.top_p ?? null;
    default:
      return model[key] ?? null;
  }
}

/** Chips that make sense for a row kind: an embedding row has no tools / params / budgets. */
export function capabilityKeysFor(kind: ModelRowKind): CapabilityKey[] {
  return kind === "embedding"
    ? ["embedding_dimension"]
    : CAPABILITY_KEYS.filter((key) => key !== "embedding_dimension");
}

/** Per-field provenance; a missing entry inherits the row's `source`. */
export function fieldSource(model: ModelRowOut, key: CapabilityKey): ModelCapabilitySource {
  return model.capability_sources?.[key] ?? model.source ?? "manual";
}

export interface Referrer {
  kind: string;
  id: string;
  name?: string | null;
}

/** The referrers a 409 `model.in_use` / `model_provider.in_use` lists in `detail`
 *  (`{referrers: [{kind, id, name}]}`; a bare list and `{agents: [...]}` are tolerated). */
export function referrersFromError(err: unknown): Referrer[] {
  if (!(err instanceof ApiError)) return [];
  const detail = err.detail as unknown;
  const raw: unknown = Array.isArray(detail)
    ? detail
    : detail && typeof detail === "object"
      ? ((detail as { referrers?: unknown }).referrers ??
        Object.entries(detail as Record<string, unknown>)
          .filter(([, v]) => Array.isArray(v))
          .flatMap(([k, v]) => (v as unknown[]).map((item) => ({ kind: k.replace(/s$/, ""), item }))))
      : null;
  if (!Array.isArray(raw)) return [];
  return raw
    .map((entry): Referrer | null => {
      if (typeof entry === "string") return { kind: "agent", id: entry };
      if (!entry || typeof entry !== "object") return null;
      const wrapped = entry as { kind?: string; item?: unknown; id?: string; name?: string | null };
      const item = (wrapped.item ?? entry) as { kind?: string; id?: string; name?: string | null };
      if (typeof item === "string") return { kind: wrapped.kind ?? "agent", id: item };
      if (!item.id) return null;
      return { kind: item.kind ?? wrapped.kind ?? "agent", id: item.id, name: item.name ?? null };
    })
    .filter((r): r is Referrer => r !== null);
}

/** N17-obs207: `detail.roles[].role` of a 409 `model_provider.in_use` — the platform roles holding it. */
export function rolesFromError(err: unknown): string[] {
  if (!(err instanceof ApiError)) return [];
  const roles = (err.detail as { roles?: unknown } | null)?.roles;
  return Array.isArray(roles) ? roles.map((r) => (r as { role?: string }).role ?? "").filter(Boolean) : [];
}

/** N17-obs207: the system presets (agents with a `system_key`) among `detail.agents`. */
export function presetsFromError(err: unknown): string[] {
  if (!(err instanceof ApiError)) return [];
  const agents = (err.detail as { agents?: unknown } | null)?.agents;
  return Array.isArray(agents)
    ? agents.filter((a) => (a as { system_key?: string | null }).system_key).map((a) => (a as { name?: string; system_key?: string }).name ?? (a as { system_key?: string }).system_key ?? "")
    : [];
}

export function isInUse(err: unknown): boolean {
  return err instanceof ApiError && (err.code === "model.in_use" || err.code === "model_provider.in_use");
}

/** M1-b lands the probe/credential handlers after the API: until then the routes answer 501. */
export function isProbeUnavailable(err: unknown): boolean {
  return (
    err instanceof ApiError &&
    (err.code === "model_providers.probe_unavailable" ||
      err.code === "model_providers.credential_unavailable" ||
      err.code === "http.501")
  );
}

export function isProbeFailed(err: unknown): boolean {
  return err instanceof ApiError && err.code === "model.probe_failed";
}

/** Console link to the surface a referrer lives on. */
export function referrerLink(ref: Referrer): string {
  switch (ref.kind) {
    case "evaluator":
      return `/v2/eval/evaluators?view=detail&id=${encodeURIComponent(ref.id)}`;
    case "setting":
      return "/v2/eval/evaluators";
    default:
      return `/v2/agents?view=detail&id=${encodeURIComponent(ref.id)}`;
  }
}

export function providerOf(providers: ModelProviderOut[], id: string | null): ModelProviderOut | undefined {
  return providers.find((p) => p.id === id);
}

/** Journal summary: keys whose value differs between `before` and `after` (either side may be null). */
export function changedKeys(before: Record<string, unknown> | null, after: Record<string, unknown> | null): string[] {
  const keys = new Set([...Object.keys(before ?? {}), ...Object.keys(after ?? {})]);
  return [...keys].filter((k) => JSON.stringify(before?.[k] ?? null) !== JSON.stringify(after?.[k] ?? null)).sort();
}

/** Numeric form input → integer or null (blank). */
export function intOrNull(raw: string): number | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const n = Math.round(Number(trimmed));
  return Number.isFinite(n) && n > 0 ? n : null;
}

/** Credential status of a provider card: host-env (built-in Bedrock) · configured · missing. */
export type CredentialStatus = "host_env" | "configured" | "missing";

export function credentialStatus(p: ModelProviderOut): CredentialStatus {
  if (p.credential_source === "host_env") return "host_env";
  return p.configured || !!p.key_last4 ? "configured" : "missing";
}

/** N17-obs203 §3: the last four characters of a key typed in the console, if any. */
export function keyLast4(p: ModelProviderOut): string | null {
  return p.key_last4 ?? null;
}

/** N17-obs252: whether a model role's console feature is hidden right now — the Skill Lab
 *  roles follow `HIDDEN_SURFACES.features`, the Studio coding agent follows
 *  `HIDDEN_SURFACES.createEntries.studio`; a role without a feature is always shown. */
export function roleHidden(feature: string | null | undefined): boolean {
  if (!feature) return false;
  if (feature === "studio") return !entryShown("studio" satisfies CreateEntry);
  return featureHidden(feature as Feature);
}

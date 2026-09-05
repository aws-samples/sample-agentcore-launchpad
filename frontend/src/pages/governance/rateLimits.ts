import type {
  GovernanceRateLimit,
  GovernanceRateLimitEntry,
  GovernanceRateMetric,
  GovernanceRatePeriod,
} from "../../lib/api";

/**
 * Client-side mirror of the backend rate-limit rules
 * (`backend/app/services/governance.py::validate_rate_limit_spec`). The server
 * is authoritative; this exists so the SAVE button can explain what is wrong
 * before a request is sent.
 */

export const RATE_LIMIT_FIXED_KEYS = [
  "targetName",
  "toolName",
  "qualifiedModelId",
  "$.context.iam.principal",
  "$.context.iam.sourceIdentity",
] as const;
export const RATE_LIMIT_JWT_PREFIX = "$.context.jwt.";
const JWT_CLAIM = /^[a-zA-Z_][a-zA-Z0-9_\-.]{0,61}[a-zA-Z0-9_]$/;

export const RATE_LIMIT_METRICS: GovernanceRateMetric[] = ["requests", "tokens", "connections"];
export const RATE_LIMIT_PERIODS: Record<GovernanceRateMetric, GovernanceRatePeriod[]> = {
  requests: ["second", "minute"],
  tokens: ["minute"],
  connections: ["second"],
};
export const RATE_LIMIT_MAX_KEYS = 10;
export const RATE_LIMIT_MAX_ENTRIES = 1000;
export const RATE_LIMIT_MAX_RATE = 10_000_000;
export const RATE_LIMIT_MAX_DESCRIPTION = 512;
export const WILDCARD = "*";

export function isJwtClaimValid(claim: string): boolean {
  return JWT_CLAIM.test(claim);
}

export function isDimensionKey(key: string): boolean {
  if ((RATE_LIMIT_FIXED_KEYS as readonly string[]).includes(key)) return true;
  return key.startsWith(RATE_LIMIT_JWT_PREFIX) && isJwtClaimValid(key.slice(RATE_LIMIT_JWT_PREFIX.length));
}

export interface MetricDraft {
  enabled: boolean;
  rate: string;
  period: GovernanceRatePeriod;
}

export interface EntryDraft {
  dimensions: Record<string, string>;
  requests: MetricDraft;
  tokens: MetricDraft;
  connections: MetricDraft;
}

export interface RateLimitDraft {
  dimensionKeys: string[];
  entries: EntryDraft[];
  description: string;
}

export type RateLimitBlocker =
  | "noKeys"
  | "tooManyKeys"
  | "noEntries"
  | "tooManyEntries"
  | "dimensionEmpty"
  | "wildcardNotTrailing"
  | "entryNoMetric"
  | "rateInvalid"
  | "periodNotAllowed"
  | "descriptionTooLong";

export function emptyMetric(metric: GovernanceRateMetric): MetricDraft {
  return { enabled: false, rate: "", period: RATE_LIMIT_PERIODS[metric][0] };
}

export function emptyEntry(keys: string[]): EntryDraft {
  return {
    dimensions: Object.fromEntries(keys.map((key) => [key, WILDCARD])),
    requests: emptyMetric("requests"),
    tokens: emptyMetric("tokens"),
    connections: emptyMetric("connections"),
  };
}

export function emptyDraft(): RateLimitDraft {
  return { dimensionKeys: [], entries: [], description: "" };
}

function metricDraft(metric: GovernanceRateMetric, entry: GovernanceRateLimitEntry): MetricDraft {
  const config = entry[metric]?.[0];
  if (!config) return emptyMetric(metric);
  return { enabled: true, rate: String(config.rate), period: config.period };
}

/** Editing keeps the AWS key set and re-hydrates every entry into the editor. */
export function draftFromRateLimit(limit: GovernanceRateLimit): RateLimitDraft {
  return {
    dimensionKeys: [...limit.dimension_keys],
    description: limit.description ?? "",
    entries: limit.entries.map((entry) => ({
      dimensions: Object.fromEntries(
        limit.dimension_keys.map((key) => [key, entry.dimensions[key] ?? WILDCARD]),
      ),
      requests: metricDraft("requests", entry),
      tokens: metricDraft("tokens", entry),
      connections: metricDraft("connections", entry),
    })),
  };
}

/** `*` may only appear in trailing positions of the ordered key list. */
export function wildcardIsTrailing(values: string[]): boolean {
  let seen = false;
  for (const value of values) {
    if (value === WILDCARD) seen = true;
    else if (seen) return false;
  }
  return true;
}

function rateIsValid(rate: string): boolean {
  if (rate.trim() === "") return false;
  const value = Number(rate);
  return Number.isFinite(value) && value >= 0 && value <= RATE_LIMIT_MAX_RATE;
}

/** Ordered, de-duplicated list of what still blocks SAVE. Empty means valid. */
export function validateDraft(draft: RateLimitDraft): RateLimitBlocker[] {
  const blockers = new Set<RateLimitBlocker>();
  if (draft.dimensionKeys.length === 0) blockers.add("noKeys");
  if (draft.dimensionKeys.length > RATE_LIMIT_MAX_KEYS) blockers.add("tooManyKeys");
  if (draft.entries.length === 0) blockers.add("noEntries");
  if (draft.entries.length > RATE_LIMIT_MAX_ENTRIES) blockers.add("tooManyEntries");
  if (draft.description.length > RATE_LIMIT_MAX_DESCRIPTION) blockers.add("descriptionTooLong");
  for (const entry of draft.entries) {
    const values = draft.dimensionKeys.map((key) => (entry.dimensions[key] ?? "").trim());
    if (values.some((value) => value === "")) blockers.add("dimensionEmpty");
    else if (!wildcardIsTrailing(values)) blockers.add("wildcardNotTrailing");
    const enabled = RATE_LIMIT_METRICS.filter((metric) => entry[metric].enabled);
    if (enabled.length === 0) blockers.add("entryNoMetric");
    for (const metric of enabled) {
      if (!rateIsValid(entry[metric].rate)) blockers.add("rateInvalid");
      if (!RATE_LIMIT_PERIODS[metric].includes(entry[metric].period)) {
        blockers.add("periodNotAllowed");
      }
    }
  }
  return [...blockers];
}

/** The wire entries for a valid draft — only enabled metrics are present. */
export function entriesFromDraft(draft: RateLimitDraft): GovernanceRateLimitEntry[] {
  return draft.entries.map((entry) => {
    const wire: GovernanceRateLimitEntry = {
      dimensions: Object.fromEntries(
        draft.dimensionKeys.map((key) => [key, (entry.dimensions[key] ?? "").trim()]),
      ),
    };
    for (const metric of RATE_LIMIT_METRICS) {
      const config = entry[metric];
      if (config.enabled) {
        wire[metric] = [{ rate: Number(config.rate), period: config.period }];
      }
    }
    return wire;
  });
}

export function entryMetricSummary(entry: GovernanceRateLimitEntry): string[] {
  return RATE_LIMIT_METRICS.flatMap((metric) =>
    (entry[metric] ?? []).map((config) => `${metric} ${config.rate}/${config.period}`),
  );
}

/** Memory resource rules shared by the classic and V2 memory consoles —
 *  CreateMemory / UpdateMemory constraints mirrored client-side so bad input
 *  gates the button instead of failing server-side. */

/** CreateMemory's own name constraint. */
export const MEMORY_NAME_RE = /^[a-zA-Z][a-zA-Z0-9_]{0,47}$/;

/** Order matters: it is the order sent to CreateMemory and shown as chips. */
export const MEMORY_STRATEGY_KEYS = ["semantic", "user_preference", "summarization"] as const;
export type MemoryStrategyKey = (typeof MEMORY_STRATEGY_KEYS)[number];
export const DEFAULT_MEMORY_STRATEGIES: MemoryStrategyKey[] = ["semantic", "user_preference"];

/** The namespace each strategy pick actually gets — mirrors the backend's
 *  canned layout (`services/opensearch_http.py` STRATEGIES). */
export const MEMORY_STRATEGY_NAMESPACES: Record<MemoryStrategyKey, string> = {
  semantic: "/facts/{actorId}",
  user_preference: "/preferences/{actorId}",
  summarization: "/summaries/{actorId}/{sessionId}",
};

/** Event-expiry range: CreateMemory accepts 3 days, UpdateMemory holds a floor of 7. */
export const CREATE_EXPIRY_MIN = 3;
export const EDIT_EXPIRY_MIN = 7;
export const EXPIRY_MAX = 365;
export const DEFAULT_EXPIRY_DAYS = 30;

export function expiryValid(days: number, min: number): boolean {
  return Number.isInteger(days) && days >= min && days <= EXPIRY_MAX;
}

/* ── merge strategy v2 (r14-a, N14-obs1) — mirrors `opensearch_http.MERGE_SETTINGS_*` ── */

export const MERGE_KEYS = [
  "recall_half_life_days",
  "max_facts_per_actor",
  "max_preferences_per_actor",
  "long_term_ttl_days",
  "actor_summary_interval",
] as const;
export type MergeKey = (typeof MERGE_KEYS)[number];

export const MERGE_DEFAULTS: Record<MergeKey, number | null> = {
  recall_half_life_days: 90,
  max_facts_per_actor: 200,
  max_preferences_per_actor: 100,
  long_term_ttl_days: null,
  actor_summary_interval: 1,
};

export const MERGE_BOUNDS: Record<MergeKey, [number, number]> = {
  recall_half_life_days: [1, 3650],
  max_facts_per_actor: [10, 10000],
  max_preferences_per_actor: [10, 10000],
  long_term_ttl_days: [7, 3650],
  actor_summary_interval: [1, 100],
};

/** The knobs that may be left empty (= the backend default / "keep forever"). */
export const MERGE_NULLABLE: ReadonlySet<MergeKey> = new Set<MergeKey>(["long_term_ttl_days"]);

/** A form field's text → the value to send: `null` for an empty nullable field, a whole
 *  number inside the bounds, or `undefined` when invalid. */
export function parseMergeValue(key: MergeKey, raw: string): number | null | undefined {
  const text = raw.trim();
  if (text === "") return MERGE_NULLABLE.has(key) ? null : undefined;
  if (!/^-?\d+$/.test(text)) return undefined;
  const value = Number(text);
  const [min, max] = MERGE_BOUNDS[key];
  return value >= min && value <= max ? value : undefined;
}

/** True when `raw` is an acceptable value for `key` (see `parseMergeValue`). */
export function mergeValueValid(key: MergeKey, raw: string): boolean {
  return parseMergeValue(key, raw) !== undefined;
}

/** The merge patch to send: only keys whose parsed value differs from `current`
 *  (the detail's knobs on edit, the defaults on create). `{}` when nothing changed. */
export function mergePatch(
  form: Record<MergeKey, string>,
  current: Record<MergeKey, number | null>,
): Partial<Record<MergeKey, number | null>> {
  const patch: Partial<Record<MergeKey, number | null>> = {};
  for (const key of MERGE_KEYS) {
    const value = parseMergeValue(key, form[key]);
    if (value === undefined) continue;
    if (value !== current[key]) patch[key] = value;
  }
  return patch;
}

/** Form text for a knob's stored value (`null` → empty). */
export function mergeFormValue(value: number | null | undefined): string {
  return value == null ? "" : String(value);
}

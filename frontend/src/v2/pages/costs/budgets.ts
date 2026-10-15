// Pure logic of the admin budget surfaces — the Budgets tab of `/v2/costs`, the budget
// card of a user's detail page and the price / AgentCore-rate editors of `/v2/models`:
// what a form holds, the body it sends, and the effective limit an assignment yields.
// Mirrors `backend/app/services/budgets.py` (effective_limit, MAX_BUDGET_USD) and
// `services/rates.py` (MAX_PRICE_PER_MTOK, the cache factors) so vitest covers it.
import type {
  AgentCoreRates,
  CalibrationReport,
  ReconcileStatus,
  RuntimeFactor,
  UsageByKind,
  BudgetAssignmentInput,
  BudgetEstimateKey,
  BudgetBasis,
  BudgetLevelInfo,
  BudgetPerson,
  BudgetTotals,
  ConsoleRole,
  ModelPriceEntry,
  ModelPriceInput,
  ModelPriceRates,
  UnpricedModel,
} from "../../../lib/api";
import { usageTone } from "../../../lib/budgets";
import type { Signal } from "../../signals";

export const MAX_BUDGET_USD = 1_000_000;
export const MAX_PRICE_PER_MTOK = 10_000;
export const MAX_RATE = 1_000_000;
/** the per-unit USD estimates the settings route accepts (`BudgetSettingsIn`) */
export const MAX_TURN_ESTIMATE_USD = 100;
/** the per-turn token counts it accepts (persona actor tokens) */
export const MAX_ESTIMATE_TOKENS = 1_000_000;

export type EstimateUnit = "usd" | "tokens";

/** The admin Estimates card, in order: each key's unit and the largest value it takes. */
export const ESTIMATE_FIELDS: { key: BudgetEstimateKey; unit: EstimateUnit; max: number }[] = [
  { key: "opaque_turn_estimate_usd", unit: "usd", max: MAX_TURN_ESTIMATE_USD },
  { key: "agent_turn_estimate_usd", unit: "usd", max: MAX_TURN_ESTIMATE_USD },
  { key: "persona_actor_input_tokens", unit: "tokens", max: MAX_ESTIMATE_TOKENS },
  { key: "persona_actor_output_tokens", unit: "tokens", max: MAX_ESTIMATE_TOKENS },
  { key: "skill_lab_eval_task_usd", unit: "usd", max: MAX_TURN_ESTIMATE_USD },
  { key: "skill_lab_train_step_usd", unit: "usd", max: MAX_TURN_ESTIMATE_USD },
  { key: "skill_lab_taskgen_job_usd", unit: "usd", max: MAX_TURN_ESTIMATE_USD },
];

/** A typed estimate, or null when it is not acceptable: token counts are whole numbers. */
export function estimateFromInput(raw: string, unit: EstimateUnit, max: number): number | null {
  const value = amountFromInput(raw, max);
  if (value === null) return null;
  return unit === "tokens" && !Number.isInteger(value) ? null : value;
}

/** A typed non-negative amount up to `max`, or null when it is not acceptable
 *  (empty included — the caller decides what an empty field means). */
export function amountFromInput(raw: string, max: number = MAX_BUDGET_USD): number | null {
  const text = raw.trim();
  if (!text) return null;
  const n = Number(text);
  if (!Number.isFinite(n) || n < 0 || n > max) return null;
  return n;
}

/** A stored amount as a form types it (no float noise such as 0.30000000000000004). */
export function amountToInput(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "";
  return String(Number(value.toPrecision(12)));
}

/* ---------- levels ---------- */

export interface LevelDraft {
  name: string;
  amount: string;
  isDefault: boolean;
}

export function levelDraftFrom(level: BudgetLevelInfo | null): LevelDraft {
  return level
    ? { name: level.name, amount: amountToInput(level.monthly_budget_usd), isDefault: level.is_default }
    : { name: "", amount: "", isDefault: false };
}

export function levelDraftValid(draft: LevelDraft): boolean {
  const name = draft.name.trim();
  return name.length >= 1 && name.length <= 64 && amountFromInput(draft.amount) !== null;
}

/** `POST /api/budgets/levels` body. */
export function levelCreatePayload(draft: LevelDraft): { name: string; monthly_budget_usd: number; is_default: boolean } {
  const amount = amountFromInput(draft.amount);
  if (amount === null) throw new Error("invalid amount");
  return { name: draft.name.trim(), monthly_budget_usd: amount, is_default: draft.isDefault };
}

/** `PATCH /api/budgets/levels/{id}` body — only what changed. */
export function levelPatchPayload(
  level: BudgetLevelInfo,
  draft: LevelDraft,
): { name?: string; monthly_budget_usd?: number; is_default?: boolean } {
  const patch: { name?: string; monthly_budget_usd?: number; is_default?: boolean } = {};
  const name = draft.name.trim();
  if (name !== level.name) patch.name = name;
  const amount = amountFromInput(draft.amount);
  if (amount === null) throw new Error("invalid amount");
  if (Math.abs(amount - level.monthly_budget_usd) > 1e-9) patch.monthly_budget_usd = amount;
  if (draft.isDefault !== level.is_default) patch.is_default = draft.isDefault;
  return patch;
}

/* ---------- assignment ---------- */

/** What the assignment form holds: `levelId` "" = no level, `override` "" = none. */
export interface AssignDraft {
  levelId: string;
  override: string;
}

export function assignDraftFrom(current: {
  assigned_level_id?: string | null;
  budget_level_id?: string | null;
  override_usd?: number | null;
  budget_override_usd?: number | null;
}): AssignDraft {
  return {
    levelId: current.assigned_level_id ?? current.budget_level_id ?? "",
    override: amountToInput(current.override_usd ?? current.budget_override_usd ?? null),
  };
}

/** An empty override is "none"; a typed one must be an acceptable amount. */
export function assignDraftValid(draft: AssignDraft): boolean {
  return !draft.override.trim() || amountFromInput(draft.override) !== null;
}

/** `PUT /api/budgets/users/{id}` body — the full assignment, nulls clear. */
export function assignmentPayload(draft: AssignDraft): BudgetAssignmentInput {
  if (!assignDraftValid(draft)) throw new Error("invalid override");
  return {
    level_id: draft.levelId || null,
    override_usd: draft.override.trim() ? amountFromInput(draft.override) : null,
  };
}

export interface LimitPreview {
  limit: number | null;
  basis: BudgetBasis;
  levelName: string | null;
}

/**
 * The limit an assignment yields, as `services/budgets.effective_limit` computes it:
 * override > assigned level > default level > unlimited; an admin with neither a level
 * nor an override is exempt (an admin is never put on the default level).
 */
export function previewLimit(role: ConsoleRole, draft: AssignDraft, levels: BudgetLevelInfo[]): LimitPreview {
  const level = draft.levelId ? (levels.find((l) => l.id === draft.levelId) ?? null) : null;
  const override = draft.override.trim() ? amountFromInput(draft.override) : null;
  if (role === "admin" && level === null && override === null) {
    return { limit: null, basis: "exempt_admin", levelName: null };
  }
  if (override !== null) return { limit: override, basis: "override", levelName: level?.name ?? null };
  if (level !== null) return { limit: level.monthly_budget_usd, basis: "level", levelName: level.name };
  const fallback = levels.find((l) => l.is_default) ?? null;
  if (fallback) return { limit: fallback.monthly_budget_usd, basis: "default_level", levelName: fallback.name };
  return { limit: null, basis: "unlimited", levelName: null };
}

/* ---------- the people table ---------- */

export type PeopleSortKey = "person" | "used" | "remaining";
export interface PeopleSort {
  key: PeopleSortKey;
  dir: "asc" | "desc";
}

/** Remaining as a sort value: unlimited sorts after every limited person. */
function remainingOf(p: BudgetPerson): number {
  return p.remaining_usd ?? Number.POSITIVE_INFINITY;
}

/** Filter by a username substring, then sort (ties by username). */
export function sortPeople(people: BudgetPerson[], sort: PeopleSort, query = ""): BudgetPerson[] {
  const q = query.trim().toLowerCase();
  const rows = q ? people.filter((p) => p.username.toLowerCase().includes(q)) : [...people];
  const sign = sort.dir === "asc" ? 1 : -1;
  return rows.sort((a, b) => {
    let diff = 0;
    if (sort.key === "used") diff = a.used_usd - b.used_usd;
    else if (sort.key === "remaining") {
      const ra = remainingOf(a);
      const rb = remainingOf(b);
      diff = ra === rb ? 0 : ra < rb ? -1 : 1;
    }
    if (diff !== 0) return diff * sign;
    const byName = a.username.localeCompare(b.username);
    return sort.key === "person" ? byName * sign : byName;
  });
}

export interface PeopleSummary {
  /** every account's spend plus what no account was charged */
  totalUsd: number;
  estimatedUsd: number;
  /** 0–1 */
  estimatedShare: number;
  budgeted: number;
  warn: number;
  over: number;
}

export function peopleSummary(people: BudgetPerson[], unattributed: BudgetTotals[]): PeopleSummary {
  const all: BudgetTotals[] = [...people, ...unattributed];
  const totalUsd = all.reduce((sum, row) => sum + row.used_usd, 0);
  const estimatedUsd = all.reduce((sum, row) => sum + row.estimated_usd, 0);
  return {
    totalUsd,
    estimatedUsd,
    estimatedShare: totalUsd > 0 ? estimatedUsd / totalUsd : 0,
    budgeted: people.filter((p) => !p.unlimited).length,
    warn: people.filter((p) => usageTone(p) === "warn").length,
    over: people.filter((p) => usageTone(p) === "over").length,
  };
}

/* ---------- model prices ---------- */

export type PriceField = keyof ModelPriceRates;
export const PRICE_FIELDS: PriceField[] = ["input", "output", "cache_read", "cache_write"];

/** What the price form holds: `modelId` is typed only when adding a new key. Empty
 *  cache rates mean "input × the cache factor" (the server default). */
export interface PriceDraft {
  modelId: string;
  input: string;
  output: string;
  cache_read: string;
  cache_write: string;
}

/** A price-map key: a model id, an ARN or a shorter family key (`rates.PRICE_KEY_RE`). */
const PRICE_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._:/@+-]{1,199}$/;

export function isPriceKey(value: string): boolean {
  return PRICE_KEY_RE.test(value.trim());
}

export function priceDraftFrom(modelId: string, rates: ModelPriceRates | null): PriceDraft {
  return {
    modelId,
    input: amountToInput(rates?.input),
    output: amountToInput(rates?.output),
    cache_read: amountToInput(rates?.cache_read),
    cache_write: amountToInput(rates?.cache_write),
  };
}

function priceOf(raw: string): number | null {
  return amountFromInput(raw, MAX_PRICE_PER_MTOK);
}

export function priceDraftValid(draft: PriceDraft): boolean {
  if (!isPriceKey(draft.modelId)) return false;
  if (priceOf(draft.input) === null || priceOf(draft.output) === null) return false;
  return (["cache_read", "cache_write"] as const).every((f) => !draft[f].trim() || priceOf(draft[f]) !== null);
}

/** `PUT /api/admin/model-prices/{id}` body; an empty cache rate is omitted. */
export function pricePayload(draft: PriceDraft): ModelPriceInput {
  if (!priceDraftValid(draft)) throw new Error("invalid price");
  const body: ModelPriceInput = { input: priceOf(draft.input) as number, output: priceOf(draft.output) as number };
  for (const field of ["cache_read", "cache_write"] as const) {
    if (draft[field].trim()) body[field] = priceOf(draft[field]) as number;
  }
  return body;
}

/** A cache rate as priced: the explicit one, else input × the factor (`derived`). */
export function cacheRate(
  rates: ModelPriceRates | null,
  field: "cache_read" | "cache_write",
  factors: { cache_read: number; cache_write: number },
): { value: number | null; derived: boolean } {
  if (!rates) return { value: null, derived: false };
  if (rates[field] != null) return { value: rates[field], derived: false };
  if (rates.input == null) return { value: null, derived: false };
  return { value: Number((rates.input * factors[field]).toPrecision(12)), derived: true };
}

/** One row of the price table: a priced key, or a model seen with no price. */
export interface PriceRow {
  model_id: string;
  entry: ModelPriceEntry | null;
  /** where an unpriced model was seen (`usage`, `agents`, `roles`, `spans`) */
  unpriced: string[] | null;
}

/** The server's unpriced list plus the cost report's span-derived ids (`spans`). */
export function mergeUnpriced(server: UnpricedModel[], spanIds: string[]): UnpricedModel[] {
  const out = new Map<string, Set<string>>();
  for (const row of server) out.set(row.model_id, new Set(row.seen_in));
  for (const id of spanIds) {
    if (!id || id === "(unknown model)") continue;
    const seen = out.get(id) ?? new Set<string>();
    seen.add("spans");
    out.set(id, seen);
  }
  return [...out.entries()]
    .map(([model_id, seen]) => ({ model_id, seen_in: [...seen].sort() }))
    .sort((a, b) => a.model_id.localeCompare(b.model_id));
}

/**
 * Unpriced models first (they block budgeted usage), then console overrides, then
 * every other key; `query` filters by a case-insensitive substring of the id. A span id
 * an entry already prices (it was priced since the report ran) is not listed twice.
 */
export function priceRows(prices: ModelPriceEntry[], unpriced: UnpricedModel[], query = ""): PriceRow[] {
  const q = query.trim().toLowerCase();
  const keys = new Set(prices.map((p) => p.model_id));
  const rows: PriceRow[] = [
    ...unpriced
      .filter((u) => !keys.has(u.model_id))
      .map((u) => ({ model_id: u.model_id, entry: null, unpriced: u.seen_in })),
    ...prices
      .filter((p) => p.overridden)
      .sort((a, b) => a.model_id.localeCompare(b.model_id))
      .map((p) => ({ model_id: p.model_id, entry: p, unpriced: null })),
    ...prices
      .filter((p) => !p.overridden)
      .sort((a, b) => a.model_id.localeCompare(b.model_id))
      .map((p) => ({ model_id: p.model_id, entry: p, unpriced: null })),
  ];
  return q ? rows.filter((r) => r.model_id.toLowerCase().includes(q)) : rows;
}

/* ---------- AgentCore rates + calibration ---------- */

export type RateGroup = "rates" | "calibration";

export interface RateRow {
  group: RateGroup;
  key: string;
  value: number;
  default: number | null;
  unit: string;
  /** differs from the seeded default (a stored value equal to it reads as default) */
  changed: boolean;
}

/** The rows of one group, in the server's order (it groups related keys). */
export function rateRows(view: AgentCoreRates, group: RateGroup): RateRow[] {
  const values = group === "rates" ? view.rates : view.calibration;
  const defaults = group === "rates" ? view.defaults : view.calibration_defaults;
  const units = group === "rates" ? view.units : view.calibration_units;
  return Object.entries(values).map(([key, value]) => {
    const fallback = defaults[key] ?? null;
    return {
      group,
      key,
      value,
      default: fallback,
      unit: units[key] ?? "",
      changed: fallback !== null && Math.abs(value - fallback) > Math.abs(fallback) * 1e-9 + 1e-15,
    };
  });
}

/** `PUT /api/admin/agentcore-rates` body for one key (null = reset to the default). */
export function ratePatch(group: RateGroup, key: string, value: number | null): Record<string, Record<string, number | null>> {
  return { [group]: { [key]: value } };
}

/** The i18n key of a unit string the server sends (`vCPU-hour` → `…unit.vcpu_hour`);
 *  an unknown unit falls back to the server's own text. */
export function unitKey(unit: string): string {
  return `v2.models.rates.unit.${unit.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "")}`;
}

/* ---------- reconciliation (services/budget_reconcile.py, budget_scheduler.py) ---------- */

export type ReconcileHealth = "off" | "waiting" | "failing" | "lagging" | "ok";

/** A window more than this many intervals behind (plus the 2-minute lag) is late. */
export const LAG_INTERVALS = 3;

/**
 * How the reconcile card reads: switched off (the env escape hatch), waiting for its
 * first tick, failing (the last tick had a failed part), lagging (its last window ended
 * more than `LAG_INTERVALS` ticks ago), or healthy.
 */
export function reconcileHealth(status: ReconcileStatus): ReconcileHealth {
  if (!status.enabled) return "off";
  if (!status.last_tick) return "waiting";
  if (status.last_tick.failures.length > 0) return "failing";
  const late = LAG_INTERVALS * status.interval_seconds + 120;
  if (status.lag_seconds != null && status.lag_seconds > late) return "lagging";
  return "ok";
}

export const RECONCILE_SIGNAL: Record<ReconcileHealth, Signal> = {
  off: "act",
  waiting: "wait",
  failing: "act",
  lagging: "wait",
  ok: "ok",
};

/** Whole minutes behind, at least 0; null before the first tick. */
export function lagMinutes(seconds: number | null | undefined): number | null {
  if (seconds == null || !Number.isFinite(seconds)) return null;
  return Math.max(0, Math.round(seconds / 60));
}

/** The kinds reconciliation writes (AgentCore fees), in the order the card lists them. */
export const AGENTCORE_FEE_KINDS = [
  "runtime_compute",
  "evaluation_builtin",
  "evaluation_custom",
  "web_search",
  "memory_event",
  "memory_retrieval",
  "gateway_call",
  "kb_query",
  "code_interpreter",
];

export interface KindRow {
  kind: string;
  usd: number;
  measuredUsd: number;
  estimatedUsd: number;
  /** 0–1 */
  estimatedShare: number;
  agentcore: boolean;
}

/** This period's spend by kind: model tokens and estimates first, then the AgentCore
 *  fees in their fixed order; kinds with nothing charged are dropped. */
export function kindRows(byKind: UsageByKind[]): KindRow[] {
  const order = (kind: string) => {
    const index = AGENTCORE_FEE_KINDS.indexOf(kind);
    return index < 0 ? -1 : index;
  };
  return byKind
    .filter((row) => row.usd > 0 || row.events > 0)
    .map((row) => ({
      kind: row.kind,
      usd: row.usd,
      estimatedUsd: row.estimated_usd,
      measuredUsd: Math.max(0, row.usd - row.estimated_usd),
      estimatedShare: row.usd > 0 ? row.estimated_usd / row.usd : 0,
      agentcore: AGENTCORE_FEE_KINDS.includes(row.kind),
    }))
    .sort((a, b) => order(a.kind) - order(b.kind) || b.usd - a.usd || a.kind.localeCompare(b.kind));
}

export interface FactorRow extends RuntimeFactor {
  harness: boolean;
  /** the seed this runtime would use without a measurement */
  seedGb: number;
  seedVcpu: number;
}

/** The measured factors next to the seed each would replace (Harness vs other). */
export function factorRows(status: ReconcileStatus): FactorRow[] {
  const d = status.calibration.defaults;
  return status.calibration.factors.map((f) => {
    const harness = f.runtime_name.startsWith("harness_");
    return {
      ...f,
      harness,
      seedGb: harness ? d.runtime_harness_gb : d.runtime_default_gb,
      seedVcpu: harness ? d.runtime_harness_vcpu : d.runtime_default_vcpu,
    };
  });
}

/** One account of the Cost Explorer comparison: the totals and the ledger's share of the
 *  bill (null when Cost Explorer could not be read or shows nothing). */
export function reportShare(account: CalibrationReport["accounts"][number]): number | null {
  const bill = account.cost_explorer_total_usd;
  if (bill == null || bill <= 0) return null;
  return account.ledger_total_usd / bill;
}

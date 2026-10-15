// Pure logic shared by every surface that shows spend budgets (`services/budgets.py`):
// the two 402 refusals and the copy they become, the UTC calendar-month period, money
// and percent formatting, the "usage ≥ 80 %" banner rule, and the price editor's path.
// Type-only imports keep it loadable in the node test environment (lib/api.ts pulls in
// i18n and imports this module for `localizedMessage`).
import type { BudgetBasis, MyUsage } from "./api";

/** `402` at or over the monthly limit: `{used_usd, limit_usd, period, resets_at, level}`. */
export const BUDGET_EXCEEDED = "budget.exceeded";
/** `402` for a budgeted payer about to use a model with no price: `{model_id}`. */
export const BUDGET_MODEL_UNPRICED = "budget.model_unpriced";
export type BudgetRefusalCode = typeof BUDGET_EXCEEDED | typeof BUDGET_MODEL_UNPRICED;

/** Window event a 402 dispatches (lib/api.ts): the shell re-reads My usage on it. */
export const BUDGET_REFUSED_EVENT = "launchpad-budget-refused";

export function isBudgetCode(code: unknown): code is BudgetRefusalCode {
  return code === BUDGET_EXCEEDED || code === BUDGET_MODEL_UNPRICED;
}

/** The refusal code of an error (an `ApiError`, read by shape), or null. Neither 402 is
 *  retryable: callers show it and stop, unlike a 429 throttle. */
export function budgetRefusalOf(err: unknown): BudgetRefusalCode | null {
  if (typeof err !== "object" || err === null) return null;
  const code = (err as { code?: unknown }).code;
  return isBudgetCode(code) ? code : null;
}

/** Tell the shell a request was refused for budget (no-op outside a browser). */
export function notifyBudgetRefused(code: string): void {
  if (typeof window === "undefined" || typeof CustomEvent === "undefined") return;
  window.dispatchEvent(new CustomEvent(BUDGET_REFUSED_EVENT, { detail: { code } }));
}

/* ---------- formatting ---------- */

/** Dollars; a null amount is "—", never $0. Sub-cent amounts keep four decimals. */
export function fmtUsd(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (value > 0 && value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** A unit rate, which may be far below a cent ($0.000005 per Gateway call): up to six
 *  significant digits, never in exponent notation. */
export function fmtRate(value: number | null | undefined, currency = true): string {
  if (value == null || !Number.isFinite(value)) return "—";
  const text = value.toLocaleString("en-US", { maximumSignificantDigits: 6 });
  return currency ? `$${text}` : text;
}

/** A percentage the backend already scaled to 0–100 (`percent_used`). */
export function fmtPercent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${Number(value.toFixed(1))}%`;
}

/** A 0–1 share (`estimated_share`) as a percentage. */
export function fmtShare(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return fmtPercent(value * 100);
}

/** The reset instant (00:00 UTC on the next month's 1st) as its UTC date. */
export function fmtResetDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const at = new Date(iso);
  return Number.isNaN(at.getTime()) ? iso : at.toISOString().slice(0, 10);
}

/* ---------- the period: calendar month, UTC ---------- */

const PERIOD_RE = /^\d{4}-(0[1-9]|1[0-2])$/;

export function isPeriod(value: string | null | undefined): value is string {
  return !!value && PERIOD_RE.test(value);
}

/** `YYYY-MM` of the UTC month `now` falls in. */
export function currentPeriod(now: Date = new Date()): string {
  return `${now.getUTCFullYear()}-${String(now.getUTCMonth() + 1).padStart(2, "0")}`;
}

/** The current period and the `count - 1` before it, newest first (the period picker). */
export function recentPeriods(now: Date = new Date(), count = 12): string[] {
  const out: string[] = [];
  let year = now.getUTCFullYear();
  let month = now.getUTCMonth() + 1;
  for (let i = 0; i < count; i++) {
    out.push(`${year}-${String(month).padStart(2, "0")}`);
    month -= 1;
    if (month === 0) {
      month = 12;
      year -= 1;
    }
  }
  return out;
}

/* ---------- limits, bases and the banner ---------- */

/** The i18n key explaining how a limit was reached (or why there is none). */
export function basisKey(basis: BudgetBasis): string {
  return `v2.usage.basis.${basis}`;
}

/** No limit applies: no level and no default, an admin without a level, or a principal
 *  with no account (the config admin, the auth-disabled operator). */
export function isExemptBasis(basis: BudgetBasis): boolean {
  return basis === "unlimited" || basis === "exempt_admin" || basis === "exempt_principal";
}

/** The i18n key of a `usage_events.source`; unknown sources fall back to their id. */
export function sourceKey(source: string): string {
  return `v2.usage.source.${source}`;
}

export type UsageTone = "ok" | "warn" | "over";

/** The usage banner shows from this share of the limit on. */
export const BANNER_PERCENT = 80;

type Totals = Pick<MyUsage, "unlimited" | "limit_usd" | "used_usd" | "percent_used">;

/** Over at or above the limit (what the gate refuses), warn from 80 %. */
export function usageTone(usage: Totals): UsageTone {
  if (usage.unlimited || usage.limit_usd == null) return "ok";
  if (usage.used_usd >= usage.limit_usd) return "over";
  if ((usage.percent_used ?? 0) >= BANNER_PERCENT) return "warn";
  return "ok";
}

/** The width of a usage bar, 0–100. */
export function barPercent(usage: Totals): number {
  if (usage.unlimited || usage.limit_usd == null) return 0;
  if (usage.limit_usd <= 0) return 100;
  return Math.max(0, Math.min(100, (usage.used_usd / usage.limit_usd) * 100));
}

export type BannerLevel = "warn" | "exceeded";

/** The shell banner: none for an unlimited person or under 80 %. */
export function bannerLevel(usage: Totals): BannerLevel | null {
  const tone = usageTone(usage);
  if (tone === "over") return "exceeded";
  if (tone === "warn") return "warn";
  return null;
}

/** One dismissal per period and level: dismissing the 80 % warning still shows the
 *  "budget reached" banner once the limit is crossed. */
export function bannerDismissKey(period: string, level: BannerLevel): string {
  return `${period}:${level}`;
}

/* ---------- the refusal copy ---------- */

type Translate = (key: string, options?: Record<string, unknown>) => string;

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * The console copy of a budget refusal, or null when `code` is not one. `detail` is the
 * envelope's (it may be missing on an SSE frame or a hand-built error): without the
 * amounts the message still says what happened.
 */
export function budgetRefusalText(code: string, detail: unknown, t: Translate): string | null {
  if (!isBudgetCode(code)) return null;
  const d = (typeof detail === "object" && detail !== null ? detail : {}) as Record<string, unknown>;
  if (code === BUDGET_EXCEEDED) {
    const used = num(d.used_usd);
    const limit = num(d.limit_usd);
    const resets = typeof d.resets_at === "string" ? d.resets_at : null;
    if (used === null || limit === null || resets === null) return t("budgetRefusal.exceededPlain");
    return t("budgetRefusal.exceeded", { used: fmtUsd(used), limit: fmtUsd(limit), date: fmtResetDate(resets) });
  }
  const model = typeof d.model_id === "string" && d.model_id ? d.model_id : null;
  return model ? t("budgetRefusal.modelUnpriced", { model }) : t("budgetRefusal.modelUnpricedUnknown");
}

/* ---------- the price editor's route ---------- */

/** `/api/admin/model-prices/{model_id:path}`. A model id may carry `/` (an inference
 *  profile ARN), `:` and dots: each segment is encoded and the slashes are kept, so the
 *  path converter sees them whatever a proxy does with `%2F`. */
export function modelPricePath(modelId: string): string {
  return `/api/admin/model-prices/${modelId.split("/").map(encodeURIComponent).join("/")}`;
}

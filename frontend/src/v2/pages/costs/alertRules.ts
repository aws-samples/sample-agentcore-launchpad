// Pure logic of the Spend & alerts page (`/v2/costs`): how a rule's numbers are shown
// and typed, which windows a kind accepts, the signal a rule maps to, and the create /
// patch bodies a form produces. Mirrors `backend/app/services/alerts.py` (KINDS,
// NATURAL_COMPARISON, WINDOWS, DASHBOARD_WINDOWS) so vitest covers it without a DOM.
import type {
  AlertComparison,
  AlertKind,
  AlertRuleCreate,
  AlertRuleInfo,
  AlertRulePatch,
  CostActorRow,
  CostRange,
} from "../../../lib/api";
import { fmtUsd } from "../../../lib/budgets";
import type { Signal } from "../../signals";

export const ALERT_KINDS: AlertKind[] = [
  "error_rate",
  "latency_p95_ms",
  "online_quality",
  "spend_usd",
  "cost_mtd_usd",
];

/** The only direction each kind is bad in; the backend refuses the other one. */
export const NATURAL_COMPARISON: Record<AlertKind, AlertComparison> = {
  error_rate: "above",
  latency_p95_ms: "above",
  online_quality: "below",
  spend_usd: "above",
  cost_mtd_usd: "above",
};

export const COST_RANGES: CostRange[] = ["1h", "6h", "24h", "7d", "30d"];
/** error rate and latency read the observability dashboard, which has no 30-day range */
const DASHBOARD_WINDOWS: CostRange[] = ["1h", "6h", "24h", "7d"];

/** The windows a kind's form offers; empty when the kind ignores the window
 *  (online quality is a fixed 24 h, month-to-date spend is the month). */
export function windowOptions(kind: AlertKind): CostRange[] {
  if (kind === "error_rate" || kind === "latency_p95_ms") return DASHBOARD_WINDOWS;
  if (kind === "spend_usd") return COST_RANGES;
  return [];
}

export function kindUsesWindow(kind: AlertKind): boolean {
  return windowOptions(kind).length > 0;
}

/** The i18n key of what a kind's window column says when it ignores the window. */
export function fixedWindowKey(kind: AlertKind): string | null {
  if (kind === "online_quality") return "v2.alerts.windowFixed24h";
  if (kind === "cost_mtd_usd") return "v2.alerts.windowMonth";
  return null;
}

/** A window the kind accepts: the current one if valid, else 24h (valid for every windowed kind). */
export function coerceWindow(kind: AlertKind, window: CostRange): CostRange {
  const options = windowOptions(kind);
  return options.length === 0 || options.includes(window) ? window : "24h";
}

/** Dollars; a null price is "—", never $0 (shared with the budgets: lib/budgets.ts). */
export { fmtUsd };

/** A rule's value (threshold or last reading) in the unit an operator thinks in. */
export function fmtRuleValue(kind: AlertKind, value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  switch (kind) {
    case "error_rate":
      return `${Number((value * 100).toFixed(2))}%`;
    case "latency_p95_ms":
      return `${Math.round(value).toLocaleString("en-US")} ms`;
    case "online_quality":
      return value.toFixed(2);
    case "spend_usd":
    case "cost_mtd_usd":
      return fmtUsd(value);
  }
}

/** The unit shown next to the threshold input. */
export const THRESHOLD_UNIT: Record<AlertKind, string> = {
  error_rate: "%",
  latency_p95_ms: "ms",
  online_quality: "0–1",
  spend_usd: "USD",
  cost_mtd_usd: "USD",
};

/** A stored threshold as the form types it (an error rate is typed in percent). */
export function thresholdToInput(kind: AlertKind, threshold: number): string {
  const shown = kind === "error_rate" ? threshold * 100 : threshold;
  // 0.07 * 100 is 7.000000000000001 in floating point
  return String(Number(shown.toPrecision(12)));
}

/** The stored threshold a typed value means, or null when it is not acceptable. */
export function thresholdFromInput(kind: AlertKind, raw: string): number | null {
  const text = raw.trim();
  if (!text) return null;
  const n = Number(text);
  if (!Number.isFinite(n) || n < 0) return null;
  if (kind === "error_rate") return n > 100 ? null : Number((n / 100).toPrecision(12));
  if (kind === "online_quality" && n > 1) return null;
  return n;
}

/** One status vocabulary: a firing rule needs you, an unread one is waiting on telemetry. */
export function ruleSignal(rule: Pick<AlertRuleInfo, "enabled" | "state">): Signal {
  if (!rule.enabled) return "off";
  if (rule.state === "firing") return "act";
  if (rule.state === "ok") return "ok";
  return "wait";
}

/** The i18n key of a rule's state label. */
export function ruleStateKey(rule: Pick<AlertRuleInfo, "enabled" | "state">): string {
  return rule.enabled ? `v2.alerts.state.${rule.state}` : "v2.alerts.state.disabled";
}

/** What a rule form holds. `webhook` is the typed URL; `webhookMode` only matters when
 *  editing a rule that already has one (the URL is never returned to the console). */
export interface RuleDraft {
  kind: AlertKind;
  name: string;
  threshold: string;
  window: CostRange;
  webhook: string;
  webhookMode: "keep" | "replace" | "remove";
}

export function emptyDraft(kind: AlertKind = "error_rate"): RuleDraft {
  return { kind, name: "", threshold: "", window: "24h", webhook: "", webhookMode: "keep" };
}

export function draftFromRule(rule: AlertRuleInfo): RuleDraft {
  return {
    kind: rule.kind,
    name: rule.name,
    threshold: thresholdToInput(rule.kind, rule.threshold),
    window: rule.window,
    webhook: "",
    webhookMode: "keep",
  };
}

/** The webhook URL a form would send, or null when the field has nothing to send. */
function typedWebhook(draft: RuleDraft): string | null {
  const url = draft.webhook.trim();
  return url ? url : null;
}

/** Is the form submittable (the server still runs the authoritative checks)? */
export function draftValid(draft: RuleDraft, editing: AlertRuleInfo | null = null): boolean {
  if (thresholdFromInput(draft.kind, draft.threshold) === null) return false;
  if (editing?.has_webhook && draft.webhookMode === "replace" && !typedWebhook(draft)) return false;
  return true;
}

/** `POST /api/alerts` body. The comparison is left to the server's natural direction. */
export function createPayload(draft: RuleDraft): AlertRuleCreate {
  const threshold = thresholdFromInput(draft.kind, draft.threshold);
  if (threshold === null) throw new Error("invalid threshold");
  const body: AlertRuleCreate = { kind: draft.kind, name: draft.name.trim(), threshold };
  if (kindUsesWindow(draft.kind)) body.window = coerceWindow(draft.kind, draft.window);
  const url = typedWebhook(draft);
  if (url) body.webhook_url = url;
  return body;
}

/**
 * `PATCH /api/alerts/{id}` body — only what changed. The webhook:
 * - rule with a webhook: `keep` sends nothing, `replace` sends the typed URL,
 *   `remove` sends null;
 * - rule without one: a typed URL adds it, an empty field sends nothing.
 */
export function patchPayload(rule: AlertRuleInfo, draft: RuleDraft): AlertRulePatch {
  const patch: AlertRulePatch = {};
  const name = draft.name.trim();
  if (name !== rule.name) patch.name = name;
  const threshold = thresholdFromInput(rule.kind, draft.threshold);
  if (threshold === null) throw new Error("invalid threshold");
  if (Math.abs(threshold - rule.threshold) > 1e-12) patch.threshold = threshold;
  if (kindUsesWindow(rule.kind)) {
    const window = coerceWindow(rule.kind, draft.window);
    if (window !== rule.window) patch.window = window;
  }
  const url = typedWebhook(draft);
  if (rule.has_webhook) {
    if (draft.webhookMode === "remove") patch.webhook_url = null;
    else if (draft.webhookMode === "replace" && url) patch.webhook_url = url;
  } else if (url) {
    patch.webhook_url = url;
  }
  return patch;
}

/** Distinct sessions in a report — the per-session query's rows (it is capped at 1000). */
export function totalSessions(actors: CostActorRow[]): { count: number; capped: boolean } {
  const count = actors.reduce((sum, row) => sum + row.sessions, 0);
  return { count, capped: count >= 1000 };
}

import type { TFunction } from "i18next";

/**
 * Gate semantics of an evaluation plan shared by the plan and proposal views (WB-006):
 * a recommendation entry may be a plain sentence or the structured object
 * `{text, evaluator_id?, blocking?, threshold?, metric?}`; a plan recommendation row carries
 * `blocking` / `threshold` / `metric` / `intent_source` written by the backend draft.
 */

export type GateSource = "stated" | "parsed";

export interface GateFacts {
  blocking: boolean;
  threshold: number | null;
  metric: string | null;
  source: GateSource | null;
}

/** A proposal `evaluator_recommendations[]` entry (string or structured object). */
export type RecommendationEntry =
  | string
  | {
      text?: unknown;
      evaluator_id?: unknown;
      blocking?: unknown;
      threshold?: unknown;
      metric?: unknown;
      note?: unknown;
    };

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" && v.trim() ? v : null);

/** The sentence of a proposal recommendation entry — never "[object Object]". */
export function recommendationText(entry: unknown): string {
  if (typeof entry === "string") return entry;
  if (entry && typeof entry === "object" && typeof (entry as { text?: unknown }).text === "string") {
    return (entry as { text: string }).text;
  }
  return entry === null || entry === undefined ? "" : String(entry);
}

/** The repair note a normalised object entry carries (`proposal_repaired: …`), if any. */
export function recommendationNote(entry: unknown): string | null {
  if (!entry || typeof entry !== "object") return null;
  return str((entry as { note?: unknown }).note);
}

/** The gate a STRUCTURED proposal entry states; `null` for strings and gate-less objects
 *  (the backend parses prose markers — the console never guesses from text). */
export function recommendationGate(entry: unknown): GateFacts | null {
  if (!entry || typeof entry !== "object") return null;
  const e = entry as { blocking?: unknown; threshold?: unknown; metric?: unknown };
  const blocking = typeof e.blocking === "boolean" ? e.blocking : null;
  const threshold = num(e.threshold);
  if (blocking === null && threshold === null) return null;
  return { blocking: blocking === true, threshold, metric: str(e.metric), source: "stated" };
}

/** The gate a PLAN recommendation row carries (`blocking` / `threshold` / `metric` /
 *  `intent_source`, written by the backend draft); `null` when it states none. */
export function planRecommendationGate(row: unknown): GateFacts | null {
  if (!row || typeof row !== "object") return null;
  const r = row as { blocking?: unknown; threshold?: unknown; metric?: unknown; intent_source?: unknown };
  const blocking = typeof r.blocking === "boolean" ? r.blocking : null;
  const threshold = num(r.threshold);
  if (blocking === null && threshold === null) return null;
  const source = r.intent_source === "stated" || r.intent_source === "parsed" ? r.intent_source : null;
  return { blocking: blocking === true, threshold, metric: str(r.metric), source };
}

/** Plan recommendations that ask for a gate (blocking, or a threshold) and are still
 *  `unresolved` — the plan is invalid until each is mapped or declined. */
export function unresolvedBlockingCount(rows: unknown[]): number {
  return rows.filter((r) => {
    const status = (r as { status?: unknown } | null)?.status;
    const gate = planRecommendationGate(r);
    return status === "unresolved" && gate !== null && (gate.blocking || gate.threshold !== null);
  }).length;
}

export const formatThreshold = (n: number): string => n.toFixed(2);

export function gateLabel(t: TFunction, blocking: boolean, threshold: number | null): string {
  if (blocking) {
    return threshold === null
      ? t("assistantEval.blocking")
      : t("assistantEval.gateBlockingThreshold", { n: formatThreshold(threshold) });
  }
  return threshold === null
    ? t("assistantEval.informational")
    : t("assistantEval.gateInformationalThreshold", { n: formatThreshold(threshold) });
}


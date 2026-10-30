import type { TFunction } from "i18next";

import { evaluatorPolarity, passesBar } from "../lib/evaluators";
import type { V2Range } from "../lib/api";
import type { EvaluationScore } from "../lib/evaluation";
import { boundedInterval } from "./significance";
import { parseTimestamp, timeMs, zonedParts } from "../lib/timestamps";

export const RANGES: V2Range[] = ["1h", "6h", "24h", "7d"];

export const RANGE_HOURS: Record<V2Range, number> = { "1h": 1, "6h": 6, "24h": 24, "7d": 168 };

export function rangeLabel(t: TFunction, range: V2Range): string {
  return t(`v2.range.${range}`);
}

/** Date-time in the display zone (N17-obs463), "2026-09-22 13:31:55"; "—" for a missing value. */
export function fmtTime(value: string | null | undefined): string {
  if (!value) return "—";
  const d = parseTimestamp(value); // N17-obs461: a naive ledger time is UTC, not local
  if (!d) return value;
  const pad = (n: number) => String(n).padStart(2, "0");
  const z = zonedParts(d);
  return `${z.year}-${pad(z.month)}-${pad(z.day)} ${pad(z.hour)}:${pad(z.minute)}:${pad(z.second)}`;
}

/** Who created a resource — the username recorded at creation; "—" when unknown
 *  (legacy rows, system-created and out-of-console resources). */
export function creatorLabel(
  row: { created_by?: string | null; created_by_name?: string | null } | null | undefined,
): string {
  return row?.created_by_name || "—";
}

export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
}

export function fmtNumber(value: number | null | undefined): string {
  return value == null ? "—" : value.toLocaleString();
}

/**
 * A 0..1 evaluator score oriented so that higher is always better (penalty
 * evaluators such as Refusal score HIGH when the agent refused).
 */
export function normalizedScore(score: number, evaluatorId: string): number {
  return evaluatorPolarity(evaluatorId) < 0 ? 1 - score : score;
}

/**
 * N17-obs430: a run score's 95 % interval on the same oriented scale as `normalizedScore` —
 * clamped to its scale (`boundedInterval`), then mirrored for a penalty evaluator
 * ([lo, hi] → [1 − hi, 1 − lo] on [0, 1]; on a stored scale [a, b], about a + b) so a
 * 拒答 mean shown as 0.77 reads "0.52–0.93", not the raw "0.07–0.48". The width is
 * unchanged and lo ≤ hi still holds; a positive-polarity entry is returned as is.
 * Wherever a normalized mean is displayed next to its interval, use this.
 */
export function normalizedInterval(
  entry: Pick<EvaluationScore, "evaluatorId" | "ci95"> & Partial<Pick<EvaluationScore, "scale" | "sessions_n" | "session_mean" | "sd">>,
): [number, number] | null {
  const ci = boundedInterval(entry);
  if (!ci || evaluatorPolarity(entry.evaluatorId) >= 0) return ci;
  const [a, b] = entry.scale ?? [0, 1];
  return [a + b - ci[1], a + b - ci[0]];
}

/** Below this normalized score a result counts as a failed case (Bad Case) — the default
 *  bar; a row of an evaluator with its own bar (N17-obs425) uses `passThreshold`. */
export const BAD_CASE_THRESHOLD = 0.7;

/** Tone of a normalized score; with `evaluatorId` the row's own pass bar decides "good"
 *  (a mean, or a score with no evaluator, keeps the default bar). */
export function scoreTone(normalized: number, evaluatorId?: string): "good" | "mid" | "bad" {
  const good = evaluatorId ? passesBar(normalized, evaluatorId) : normalized >= BAD_CASE_THRESHOLD;
  return good ? "good" : normalized >= 0.4 ? "mid" : "bad";
}

export function fmtScore(value: number | null | undefined): string {
  return value == null ? "—" : value.toFixed(2);
}

/** Download rows as a UTF-8 CSV (with BOM so spreadsheet apps read CJK text). */
export function downloadCsv(filename: string, header: string[], rows: (string | number | null)[][]) {
  const cell = (v: string | number | null) => {
    const s = v == null ? "" : String(v);
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const text = [header, ...rows].map((r) => r.map(cell).join(",")).join("\r\n");
  const blob = new Blob(["﻿", text], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/** Seconds since the worker's heartbeat (never negative: host clocks differ slightly). */
export function heartbeatAge(stamp: string, now: number = Date.now()): number {
  const at = timeMs(stamp);
  return Number.isNaN(at) ? 0 : Math.max(0, Math.round((now - at) / 1000));
}

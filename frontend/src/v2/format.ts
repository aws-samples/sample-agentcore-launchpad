import type { TFunction } from "i18next";

import { evaluatorPolarity } from "../lib/evaluators";
import type { V2Range } from "../lib/api";

export const RANGES: V2Range[] = ["1h", "6h", "24h", "7d"];

export const RANGE_HOURS: Record<V2Range, number> = { "1h": 1, "6h": 6, "24h": 24, "7d": 168 };

export function rangeLabel(t: TFunction, range: V2Range): string {
  return t(`v2.range.${range}`);
}

/** Local date-time, "2026-09-22 13:31:55"; "—" for a missing value. */
export function fmtTime(value: string | null | undefined): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(
    d.getMinutes(),
  )}:${pad(d.getSeconds())}`;
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
 * evaluators such as Harmfulness score HIGH when the response is bad).
 */
export function normalizedScore(score: number, evaluatorId: string): number {
  return evaluatorPolarity(evaluatorId) < 0 ? 1 - score : score;
}

/** Below this normalized score a result counts as a failed case (Bad Case). */
export const BAD_CASE_THRESHOLD = 0.7;

export function scoreTone(normalized: number): "good" | "mid" | "bad" {
  return normalized >= BAD_CASE_THRESHOLD ? "good" : normalized >= 0.4 ? "mid" : "bad";
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

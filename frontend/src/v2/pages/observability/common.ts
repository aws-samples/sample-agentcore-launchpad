import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { ObsTraceRow, V2Range } from "../../../lib/api";
import { fmtCost } from "../../../pages/observability/format";
import { RANGES } from "../../format";

export type ObsTab = "dashboard" | "sessions" | "traces";
export const OBS_TABS: ObsTab[] = ["dashboard", "sessions", "traces"];

/** Backend trace ids are 32 lowercase hex chars (X-Ray/OTel). */
export const TRACE_ID_RE = /^[0-9a-f]{32}$/;
// Mirrors the backend SessionIdParam alphabet: external callers compose
// runtimeSessionIds like `<ulid>#feishu#<chat_id>`, so `#` `:` `.` `@` are valid.
export const SESSION_ID_RE = /^[A-Za-z0-9_\-#:.@]{8,256}$/;

// Mirrors MAX_ON_DEMAND_EVALUATORS in backend/app/services/observability.py.
export const MAX_SCORE_EVALUATORS = 5;

/** A root span running longer than this is labelled a timeout, not a plain error. */
export const TIMEOUT_MS = 30_000;

export function asRange(value: string | null): V2Range {
  return (RANGES as string[]).includes(value ?? "") ? (value as V2Range) : "24h";
}

export function asTab(value: string | null): ObsTab {
  return (OBS_TABS as string[]).includes(value ?? "") ? (value as ObsTab) : "dashboard";
}

/** Harness runtimes emit a constant stream of 2-span "InternalOperation"
 * health-check traces — noise for humans, hidden by default. */
export function isSystemTrace(r: ObsTraceRow): boolean {
  return r.root_operation === "InternalOperation" && r.llm_count === 0;
}

export function shortId(id: string | null | undefined, chars = 12): string {
  if (!id) return "—";
  return id.length > chars ? `${id.slice(0, chars)}…` : id;
}

/** Seconds since the backend cached this answer, ticking once a second. */
export function useCacheAge(ageSeconds: number | null | undefined, stamp: unknown): number | null {
  const [base, setBase] = useState<{ age: number; at: number } | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    setBase(ageSeconds == null ? null : { age: ageSeconds, at: Date.now() });
  }, [ageSeconds, stamp]);
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  if (base == null) return null;
  return Math.max(0, Math.round(base.age + (now - base.at) / 1000));
}

/** Push the "cached Ns ago" / loading hint of the active tab up to the page header. */
export function useReportCache(
  ageSeconds: number | null | undefined,
  stamp: unknown,
  loading: boolean,
  report: (hint: string | null) => void,
) {
  const { t } = useTranslation();
  const age = useCacheAge(ageSeconds, stamp);
  const hint = loading ? t("v2.common.loading") : age != null ? t("v2.observability.cachedAgo", { s: age }) : null;
  useEffect(() => {
    report(hint);
  }, [hint, report]);
}

export function copyText(text: string): Promise<void> {
  try {
    // navigator.clipboard is undefined in non-secure contexts (http LAN)
    return navigator.clipboard.writeText(text);
  } catch (err) {
    return Promise.reject(err);
  }
}

/** "≈$0.019" — advisory estimate; an unknown price (null) renders as "—". */
export function approxCost(v: number | null | undefined): string {
  return v == null ? "—" : `≈${fmtCost(v)}`;
}

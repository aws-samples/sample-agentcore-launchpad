// The platform glossary: which terms exist, how a search matches one, and which
// term the (single, shell-mounted) glossary dialog shows. Pure + a tiny store, so
// vitest covers it without a DOM. The copy lives in i18n: `glossary.<term>` (one
// plain-language sentence) and `glossary.name.<term>` (the short name).
import { useSyncExternalStore } from "react";

export const GLOSSARY_TERMS = [
  "agent",
  "harness",
  "runtime",
  "registry",
  "gateway",
  "mcp",
  "cedarPolicy",
  "knowledgeBase",
  "contextLayer",
  "memory",
  "evaluator",
  "onlineEvaluation",
  "dataset",
  "experiment",
  "canary",
  "skill",
  "a2a",
  "workspace",
  "observability",
  "trace",
] as const;

export type GlossaryTerm = (typeof GLOSSARY_TERMS)[number];

export function isGlossaryTerm(value: string): value is GlossaryTerm {
  return (GLOSSARY_TERMS as readonly string[]).includes(value);
}

/**
 * Whether a glossary row answers to `query`: a case-insensitive substring of the
 * term key, its localized name, its English name or its definition — so an
 * English query still finds a term under the zh-CN UI. An empty query matches all.
 */
export function glossaryMatches(
  term: string,
  query: string,
  label: string,
  labelEn: string,
  sentence: string,
): boolean {
  const needle = query.trim().toLowerCase();
  if (!needle) return true;
  return `${term}\n${label}\n${labelEn}\n${sentence}`.toLowerCase().includes(needle);
}

/* ── which glossary the dialog shows (one at a time, opened from anywhere) ── */

export interface GlossaryState {
  open: boolean;
  /** the row to scroll to and highlight; null = the whole list */
  term: GlossaryTerm | null;
}

const CLOSED: GlossaryState = { open: false, term: null };
let state: GlossaryState = CLOSED;
const listeners = new Set<() => void>();

function emit(next: GlossaryState) {
  state = next;
  listeners.forEach((notify) => notify());
}

/** Open the glossary, scrolled to `term` when it is a known term. */
export function openGlossary(term?: string): void {
  emit({ open: true, term: term && isGlossaryTerm(term) ? term : null });
}

export function closeGlossary(): void {
  if (state.open) emit(CLOSED);
}

export function getGlossary(): GlossaryState {
  return state;
}

function subscribe(notify: () => void): () => void {
  listeners.add(notify);
  return () => listeners.delete(notify);
}

export function useGlossary(): GlossaryState {
  return useSyncExternalStore(subscribe, getGlossary, getGlossary);
}

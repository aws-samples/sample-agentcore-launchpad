// Per-browser dismissals of V2 attention rows (v2-attention-signals, "Dismiss"). Pure apart
// from the storage it is handed: no React, no i18n. One localStorage entry holds every
// panel scope; a missing, throwing or garbled store reads as "nothing dismissed" and a
// failed write is dropped, so a panel always renders.
import { parseTimestamp } from "../lib/timestamps";
import type { Signal } from "./signals";

export const DISMISS_STORAGE_KEY = "launchpad_v2_attention_dismissed";
/** A dismissal older than this is pruned on every write and ignored on read. */
export const DISMISS_TTL_MS = 30 * 24 * 60 * 60 * 1000;

/** One panel scope's dismissals: `{ [dismissKey]: dismissedAtISO }`. */
export type ScopeDismissals = Record<string, string>;
/** The stored entry: `{ [scope]: ScopeDismissals }`. */
export type DismissStore = Record<string, ScopeDismissals>;
/** The part of `Storage` this module uses (tests pass a fake). */
export type DismissStorage = Pick<Storage, "getItem" | "setItem">;

/** The stored identity of a row: its key plus its signal, so a row whose signal changes
 *  (it worsens or recovers) shows again. */
export function dismissKey(item: { key: string; signal: Signal }): string {
  return `${item.key}|${item.signal}`;
}

/** The browser's localStorage, or null where reading the property itself throws. */
function browserStorage(): DismissStorage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const fresh = (at: unknown, now: number): at is string => {
  if (typeof at !== "string") return false;
  const ms = parseTimestamp(at)?.getTime();
  return ms !== undefined && now - ms <= DISMISS_TTL_MS;
};

/** Only well-formed dismissals younger than `DISMISS_TTL_MS`; a scope left empty is dropped. */
export function pruneStore(store: unknown, now: number): DismissStore {
  const out: DismissStore = {};
  if (!isRecord(store)) return out;
  for (const [scope, entries] of Object.entries(store)) {
    if (!isRecord(entries)) continue;
    const kept: ScopeDismissals = {};
    for (const [key, at] of Object.entries(entries)) if (fresh(at, now)) kept[key] = at;
    if (Object.keys(kept).length) out[scope] = kept;
  }
  return out;
}

/** Every scope's live dismissals; `{}` when storage is missing, throws or holds garbage. */
export function readStore(storage: DismissStorage | null = browserStorage(), now = Date.now()): DismissStore {
  if (!storage) return {};
  try {
    return pruneStore(JSON.parse(storage.getItem(DISMISS_STORAGE_KEY) ?? "{}"), now);
  } catch {
    return {};
  }
}

/** One scope's live dismissals (`{}` when unavailable). */
export function loadScope(scope: string, storage: DismissStorage | null = browserStorage(), now = Date.now()): ScopeDismissals {
  return readStore(storage, now)[scope] ?? {};
}

/** Replace one scope's dismissals and write the whole entry back with every scope pruned.
 *  Never throws: a failed write leaves the dismissal in the caller's memory only. */
export function saveScope(
  scope: string,
  entries: ScopeDismissals,
  storage: DismissStorage | null = browserStorage(),
  now = Date.now(),
): void {
  if (!storage) return;
  const store = readStore(storage, now);
  const kept = pruneStore({ [scope]: entries }, now)[scope];
  if (kept) store[scope] = kept;
  else delete store[scope];
  try {
    storage.setItem(DISMISS_STORAGE_KEY, JSON.stringify(store));
  } catch {
    // per-browser convenience only (private mode, quota)
  }
}

export function withDismissal(entries: ScopeDismissals, key: string, now = Date.now()): ScopeDismissals {
  return { ...entries, [key]: new Date(now).toISOString() };
}

export function withoutDismissal(entries: ScopeDismissals, key: string): ScopeDismissals {
  const next = { ...entries };
  delete next[key];
  return next;
}

export interface VisibleAttention<T> {
  /** the rows not dismissed, capped */
  shown: T[];
  /** the rows dismissed, in the given order */
  dismissed: T[];
  /** how many rows are not dismissed (the header count) */
  remaining: number;
}

/** Hide the dismissed rows first, then cap, so dismissing a shown row promotes the next. */
export function visibleAttention<T extends { key: string; signal: Signal }>(
  items: T[],
  dismissed: ReadonlySet<string>,
  cap?: number,
): VisibleAttention<T> {
  const kept: T[] = [];
  const hidden: T[] = [];
  for (const item of items) (dismissed.has(dismissKey(item)) ? hidden : kept).push(item);
  return {
    shown: cap === undefined ? kept : kept.slice(0, Math.max(0, cap)),
    dismissed: hidden,
    remaining: kept.length,
  };
}

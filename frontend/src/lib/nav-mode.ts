import { useSyncExternalStore } from "react";

/**
 * Which slice of the console the V2 sidebar shows.
 *
 * - `business`: the build → run essentials plus Learn, for people who only build,
 *   chat with and watch agents.
 * - `expert`: every entry, exactly the sidebar as it was before this mode existed.
 *
 * **Everyone defaults to `expert`** (decision of 2026-10-13; console-v3 defaulted
 * members to business): no one's sidebar changes until they opt in.
 *
 * It is a **view filter, not authorization**: every route stays reachable by URL
 * and through the command palette, and the backend's route policy is untouched.
 * Stored per browser like `ui-version`; an empty, invalid or blocked storage means
 * the default.
 */
export type NavMode = "business" | "expert";

export const NAV_MODE_KEY = "launchpad_nav_mode";

/** A stored value as a mode, or null when it is missing or not a known mode. */
export function parseNavMode(raw: string | null): NavMode | null {
  return raw === "business" || raw === "expert" ? raw : null;
}

/** The mode of a viewer who never chose one: expert, whatever the role. */
export function defaultNavMode(): NavMode {
  return "expert";
}

const listeners = new Set<() => void>();
// the in-memory copy keeps the toggle working when storage is unavailable
let current: NavMode = read();

function read(): NavMode {
  try {
    return parseNavMode(localStorage.getItem(NAV_MODE_KEY)) ?? defaultNavMode();
  } catch {
    return defaultNavMode();
  }
}

export function getNavMode(): NavMode {
  return current;
}

export function setNavMode(mode: NavMode): void {
  try {
    localStorage.setItem(NAV_MODE_KEY, mode);
  } catch {
    // storage unavailable (private window, blocked site data): the mode still
    // applies to this tab, it just is not remembered
  }
  if (mode === current) return;
  current = mode;
  listeners.forEach((notify) => notify());
}

function subscribe(notify: () => void): () => void {
  listeners.add(notify);
  return () => listeners.delete(notify);
}

/** Reactive `[mode, setMode]`; re-renders the caller when the mode changes. */
export function useNavMode(): [NavMode, (mode: NavMode) => void] {
  const mode = useSyncExternalStore(subscribe, getNavMode, getNavMode);
  return [mode, setNavMode];
}

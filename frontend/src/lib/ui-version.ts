import { useSyncExternalStore } from "react";

// Which console experience the operator chose: the original console (v1) or
// console V2. Both ship side by side and render the same modules: in V2 the
// classic routes (/chat, /agents, …) keep their URLs but render inside the V2
// shell, next to the native V2 pages under /v2. The choice is a per-browser
// convenience, so a blocked or empty storage simply means "v1".
export type UiVersion = "v1" | "v2";

const KEY = "launchpad_ui_version";

const listeners = new Set<() => void>();
// the in-memory copy keeps the switch working when storage is unavailable
let current: UiVersion = read();

function read(): UiVersion {
  try {
    return localStorage.getItem(KEY) === "v2" ? "v2" : "v1";
  } catch {
    return "v1";
  }
}

export function getUiVersion(): UiVersion {
  return current;
}

export function setUiVersion(version: UiVersion): void {
  try {
    localStorage.setItem(KEY, version);
  } catch {
    // storage unavailable (private window, blocked site data): the switch
    // still applies to this tab, it just is not remembered
  }
  if (version === current) return;
  current = version;
  listeners.forEach((notify) => notify());
}

function subscribe(notify: () => void): () => void {
  listeners.add(notify);
  return () => listeners.delete(notify);
}

/** The current choice; re-renders the caller when either console switches. */
export function useUiVersion(): UiVersion {
  return useSyncExternalStore(subscribe, getUiVersion);
}

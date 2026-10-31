import { useSyncExternalStore } from "react";

// Which console experience the operator chose: console V2 (the default, under
// /v2) or the redesigned console V3 (/v3). Both render the same modules: the classic
// routes (/chat, /agents, …) keep their URLs but render inside that console's shell.
// The original console (v1) is hidden: nothing offers it any more and a stored "v1"
// (an earlier "Classic console" click) reads as V2. The choice is a per-browser
// convenience: only an explicit "Try V3" (a stored "v3") opts out, so a blocked or
// empty storage means V2.
export type UiVersion = "v1" | "v2" | "v3";

const KEY = "launchpad_ui_version";

const listeners = new Set<() => void>();
// the in-memory copy keeps the switch working when storage is unavailable
let current: UiVersion = read();

function read(): UiVersion {
  try {
    const stored = localStorage.getItem(KEY);
    // V2 is the default: only an explicit choice of V3 stays there (classic is hidden)
    return stored === "v3" ? "v3" : "v2";
  } catch {
    return "v2";
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

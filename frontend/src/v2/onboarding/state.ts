// V2 onboarding — per-browser memory of what a newcomer has already seen
// (ported from console-v3's onboarding state).
//
// None of this is authorization or user data: it only decides whether the
// launch checklist is folded away (and, with the guided tour, whether the tour
// starts on its own). Storage can be blocked (private windows) or missing; every
// read then falls back to "not seen", and a tab-local copy keeps a choice
// working for the session.
import { useSyncExternalStore } from "react";

const LAUNCH_HIDDEN_KEY = "launchpad_v2_launch_hidden";

const memory = new Map<string, string>();
const listeners = new Set<() => void>();

function notify() {
  listeners.forEach((listener) => listener());
}

function get(key: string): string | null {
  try {
    return localStorage.getItem(key) ?? memory.get(key) ?? null;
  } catch {
    return memory.get(key) ?? null;
  }
}

function put(key: string, value: string | null) {
  if (value === null) memory.delete(key);
  else memory.set(key, value);
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // the in-memory copy carries the choice for this tab
  }
  notify();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/* ── the launch checklist ──────────────────────────────────────────────── */

/** Hidden per workspace: a new workspace starts its own checklist. */
export function launchHiddenKey(workspaceId: string): string {
  return `${LAUNCH_HIDDEN_KEY}:${workspaceId}`;
}

export function isLaunchHidden(workspaceId: string): boolean {
  return get(launchHiddenKey(workspaceId)) === "1";
}

export function setLaunchHidden(workspaceId: string, hidden: boolean): void {
  put(launchHiddenKey(workspaceId), hidden ? "1" : null);
}

/** Reactive `[hidden, setHidden]` for one workspace's checklist. */
export function useLaunchHidden(workspaceId: string): [boolean, (hidden: boolean) => void] {
  const snapshot = () => isLaunchHidden(workspaceId);
  const hidden = useSyncExternalStore(subscribe, snapshot, snapshot);
  return [hidden, (next: boolean) => setLaunchHidden(workspaceId, next)];
}

/**
 * Bring the checklist back (the help menu's "Launch checklist"): un-hides it for
 * `workspaceId` and leaves a scroll request the checklist takes once it is on
 * screen — also when it mounts later, after the caller navigated to Home.
 * Navigating there is the caller's job.
 */
let launchRequest = 0;
let scrollPending = false;
export function requestLaunchChecklist(workspaceId: string): void {
  launchRequest += 1;
  scrollPending = true;
  put(launchHiddenKey(workspaceId), null);
}

/** A counter bumped by every request; subscribe to it to re-check for one. */
export function getLaunchRequest(): number {
  return launchRequest;
}

export function useLaunchRequest(): number {
  return useSyncExternalStore(subscribe, getLaunchRequest, getLaunchRequest);
}

/** True once per request: the checklist scrolls itself into view when it gets it. */
export function takeLaunchScroll(): boolean {
  const pending = scrollPending;
  scrollPending = false;
  return pending;
}

/* ── the guided tour ───────────────────────────────────────────────────── */
// (next step: `launchpad_v2_tour_done` and the tour request, on the same store)

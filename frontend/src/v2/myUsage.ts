// The signed-in person's own spend this month (`GET /api/me/usage`), read once per page
// load and shared by the shell banner and the user menu. A `402 budget.*` anywhere
// (lib/api.ts and lib/assistant.ts dispatch BUDGET_REFUSED_EVENT) re-reads it, and the
// My usage page hands its fresh current-month read back here, so the banner never lags
// what the page shows. A failed read is not cached: the next mount retries. Signing in
// as someone else in the same tab drops the previous person's read.
import { useEffect, useSyncExternalStore } from "react";

import { api, type MyUsage } from "../lib/api";
import { BUDGET_REFUSED_EVENT, currentPeriod } from "../lib/budgets";

interface State {
  data: MyUsage | null;
  loading: boolean;
}

let state: State = { data: null, loading: false };
let inflight: Promise<void> | null = null;
/** who the cached read belongs to; a response for an earlier owner is dropped */
let owner: string | null = null;
let generation = 0;
let listening = false;
const listeners = new Set<() => void>();

function set(next: State) {
  state = next;
  for (const fn of listeners) fn();
}

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

/** (Re)read the current month. Concurrent calls share one request. */
export function refreshMyUsage(): Promise<void> {
  if (inflight) return inflight;
  const gen = generation;
  set({ ...state, loading: true });
  const request: Promise<void> = api
    .myUsage()
    .then((data) => {
      if (gen === generation) set({ data, loading: false });
    })
    .catch(() => {
      if (gen === generation) set({ ...state, loading: false });
    })
    .finally(() => {
      if (inflight === request) inflight = null;
    });
  inflight = request;
  return request;
}

/** A fresh read of the current period from another surface (the My usage page). */
export function primeMyUsage(data: MyUsage): void {
  if (data.period === currentPeriod()) set({ data, loading: false });
}

function listen() {
  if (listening || typeof window === "undefined") return;
  listening = true;
  window.addEventListener(BUDGET_REFUSED_EVENT, () => void refreshMyUsage());
}

/** The cached current-month usage of `who` (the signed-in name); the first caller of a
 *  page load — or of a new sign-in — triggers the read. */
export function useMyUsage(who: string): State {
  const snapshot = useSyncExternalStore(subscribe, () => state);
  useEffect(() => {
    listen();
    if (owner !== who) {
      owner = who;
      generation += 1;
      inflight = null;
      set({ data: null, loading: false });
      void refreshMyUsage();
    } else if (!state.data && !inflight) {
      void refreshMyUsage();
    }
  }, [who]);
  return snapshot;
}

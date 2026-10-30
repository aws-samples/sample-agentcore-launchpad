import { useEffect, useState } from "react";

import { api } from "../lib/api";

/**
 * N17-obs267 (user request 2026-10-22): the sidebar's 「注册中心」 entry carries the
 * workspace's PENDING_APPROVAL count for an administrator so a review is not missed.
 *
 * The count is re-read on every route change and whenever a registry page reports a change
 * (`notifyRegistryChanged()` after approve / reject / submit / delete / import) — no polling
 * loop. A failed read (registry unavailable, a member's session, an older backend) hides
 * the badge rather than showing a stale or guessed number.
 */
export const REGISTRY_CHANGED_EVENT = "launchpad:registry-changed";

/** Tell the shell a registry record changed state, so the pending badge is re-read. */
export function notifyRegistryChanged(): void {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(REGISTRY_CHANGED_EVENT));
}

/**
 * The pending count while `enabled` (an administrator, the registry feature on), else null.
 * `routeKey` (pathname + search + workspace) re-reads it on navigation.
 */
export function useRegistryPendingCount(enabled: boolean, routeKey: string): number | null {
  const [pending, setPending] = useState<number | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!enabled) return;
    const bump = () => setNonce((n) => n + 1);
    window.addEventListener(REGISTRY_CHANGED_EVENT, bump);
    return () => window.removeEventListener(REGISTRY_CHANGED_EVENT, bump);
  }, [enabled]);

  useEffect(() => {
    if (!enabled) {
      setPending(null);
      return;
    }
    let live = true;
    // N17-obs471: cancelled on unmount / re-read — a read failing during a restart would
    // otherwise keep retrying (lib/api.ts) after its shell is gone
    const ctl = new AbortController();
    api
      .registryCounts(ctl.signal)
      .then((counts) => {
        if (live) setPending(typeof counts?.pending === "number" ? counts.pending : null);
      })
      .catch(() => {
        if (live) setPending(null);
      });
    return () => {
      live = false;
      ctl.abort();
    };
  }, [enabled, routeKey, nonce]);

  return enabled ? pending : null;
}

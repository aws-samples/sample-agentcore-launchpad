/**
 * WB-003: the first stale chunk on a path reloads the tab by itself — the user
 * saw "this page's code … reload" after every host update and had to click.
 * One reload per path per tab (a sessionStorage mark), so a chunk that is still
 * missing after the reload falls back to the manual state instead of looping;
 * the mark is cleared once the page chunk renders (`ChunkLoaded`).
 */
const RELOAD_MARK = "launchpad.route-chunk.reloaded:";

/**
 * A stale-chunk error while the browser reports itself offline is a network outage,
 * not a host update: an automatic reload would only land on the browser's offline page
 * and lose the tab's state, so the boundary goes straight to the manual state (r7-e
 * review follow-up on WB-003). Nothing is marked — once the network is back, the next
 * stale chunk on this path may still auto-reload once. `navigator.onLine === true` is
 * not trusted as "online" (browsers report it for a captive portal too); only the
 * explicit `false` is acted on.
 */
export function browserIsOffline(): boolean {
  return typeof navigator !== "undefined" && navigator.onLine === false;
}

/** `window.location.reload` behind one seam, so the boundary's tests can observe it. */
export const pageReloader = { reload: () => window.location.reload() };

function markKey(pathname: string): string {
  return RELOAD_MARK + pathname;
}

export function canReloadOnce(pathname: string): boolean {
  try {
    return sessionStorage.getItem(markKey(pathname)) === null;
  } catch {
    return false; // storage blocked — the manual state shows instead
  }
}

export function markReloaded(pathname: string): void {
  try {
    sessionStorage.setItem(markKey(pathname), new Date().toISOString());
  } catch {
    // storage blocked — nothing to remember, canReloadOnce already said no
  }
}

/** The page chunk rendered: the next stale chunk on this path may auto-reload again. */
export function clearReloadMark(pathname: string): void {
  try {
    sessionStorage.removeItem(markKey(pathname));
  } catch {
    // nothing to clear
  }
}

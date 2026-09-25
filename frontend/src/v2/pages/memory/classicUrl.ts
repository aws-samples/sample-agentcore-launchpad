const TABS = ["overview", "short-term", "long-term", "resources"];

/**
 * Maps a classic `/memory` query string onto the native V2 page, so links from
 * other modules, bookmarks and hand-overs keep landing in the right sub-page.
 * Classic used `?view=` for the tab (overview | short-term | long-term |
 * resources) with the selection in `?actor=`/`?session=`/`?strategy=`; V2 keeps
 * the selection params and moves the tab to `?tab=` (V2's `?view=` is the
 * resource sub-page).
 */
export function classicMemoryToV2(search: string): string {
  const src = new URLSearchParams(search);
  const out = new URLSearchParams();
  const view = src.get("view");
  if (view && TABS.includes(view) && view !== "overview") out.set("tab", view);
  for (const key of ["actor", "session", "strategy"]) {
    const value = src.get(key);
    if (value) out.set(key, value);
  }
  const qs = out.toString();
  return qs ? `/v2/memory?${qs}` : "/v2/memory";
}

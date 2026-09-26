/**
 * Maps a classic `/workspaces` query string onto the native V2 page, so links from
 * other modules (the top-bar switcher's "manage" link), bookmarks and hand-overs
 * keep landing in the right sub-page.
 *
 * classic `?view=create` → `?view=new`; `?view=detail&ws=<id>` → `?view=detail&id=<id>`
 * (the member-grants params `gq`, `granted`, `gpage` carry over unchanged).
 */
export function classicWorkspacesToV2(search: string): string {
  const src = new URLSearchParams(search);
  const out = new URLSearchParams();
  const view = src.get("view");
  const ws = src.get("ws") ?? src.get("id");
  if (view === "create" || view === "new") {
    out.set("view", "new");
  } else if (view === "detail" && ws) {
    out.set("view", "detail");
    out.set("id", ws);
    for (const key of ["gq", "granted", "gpage"]) {
      const value = src.get(key);
      if (value) out.set(key, value);
    }
  }
  const query = out.toString();
  return `/v2/workspaces${query ? `?${query}` : ""}`;
}

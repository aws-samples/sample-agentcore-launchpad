/**
 * Maps a classic `/announcements` query string onto the native V2 page, so links from
 * other modules, bookmarks and hand-overs keep landing in the right sub-page.
 *
 * classic                   → V2
 *   (none)                  → /v2/announcements
 *   ?announcement=new       → ?view=new
 *   ?announcement=<id>      → ?view=edit&id=<id>
 */
export function classicAnnouncementsToV2(search: string): string {
  const selected = new URLSearchParams(search).get("announcement");
  const out = new URLSearchParams();
  if (selected === "new") out.set("view", "new");
  else if (selected) {
    out.set("view", "edit");
    out.set("id", selected);
  }
  const query = out.toString();
  return `/v2/announcements${query ? `?${query}` : ""}`;
}

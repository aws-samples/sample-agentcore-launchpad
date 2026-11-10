/**
 * Maps a classic `/videos` query string onto the native V2 page, so links from
 * other modules, bookmarks and hand-overs keep landing in the right sub-page.
 *
 * classic                 → V2
 *   ?video=<id>           → ?view=watch&video=<id>
 *   ?q= / ?category= / ?section= → kept as is (library filters)
 */
export function classicVideosToV2(search: string): string {
  const params = new URLSearchParams(search);
  const out = new URLSearchParams();
  for (const name of ["q", "category", "section"]) {
    const value = params.get(name);
    if (value) out.set(name, value);
  }
  const video = params.get("video");
  if (video !== null) {
    out.set("view", "watch");
    out.set("video", video);
  }
  const query = out.toString();
  return `/v2/videos${query ? `?${query}` : ""}`;
}

/**
 * `/v2/video-management` was folded into the Videos page as its administrator-only
 * `?view=manage` tab (2026-10-19, N15-obs14). Saved bookmarks and old links keep
 * landing on the same sub-page:
 *
 *   /v2/video-management                → /v2/videos?view=manage
 *   /v2/video-management?view=new       → /v2/videos?view=manage&edit=new
 *   /v2/video-management?view=edit&id=X → /v2/videos?view=manage&edit=X
 */
export function legacyVideoManagementToV2(search: string): string {
  const params = new URLSearchParams(search);
  const out = new URLSearchParams({ view: "manage" });
  const view = params.get("view");
  const id = params.get("id");
  if (view === "new") out.set("edit", "new");
  else if (view === "edit" && id) out.set("edit", id);
  return `/v2/videos?${out.toString()}`;
}

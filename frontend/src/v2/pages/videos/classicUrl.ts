/**
 * Maps a classic `/videos` query string onto the native V2 page, so links from
 * other modules, bookmarks and hand-overs keep landing in the right sub-page.
 *
 * classic                 → V2
 *   ?video=<id>           → ?view=watch&video=<id>
 *   ?q= / ?category=      → kept as is (library filters)
 */
export function classicVideosToV2(search: string): string {
  const params = new URLSearchParams(search);
  const out = new URLSearchParams();
  for (const name of ["q", "category"]) {
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

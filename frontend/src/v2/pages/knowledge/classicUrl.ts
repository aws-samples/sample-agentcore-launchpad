/**
 * Maps a classic `/knowledge-bases` query string onto the native V2 page, so links from
 * other modules, bookmarks and hand-overs keep landing in the right sub-page:
 * `?view=create` → `?view=new`, `?view=detail&kb=<id>` → `?view=detail&id=<id>`.
 */
export function classicKnowledgeBasesToV2(search: string): string {
  const src = new URLSearchParams(search);
  const out = new URLSearchParams();
  const view = src.get("view");
  if (view === "create") {
    out.set("view", "new");
  } else if (view === "detail") {
    out.set("view", "detail");
    const kb = src.get("kb") ?? src.get("id");
    if (kb) out.set("id", kb);
    const tab = src.get("tab");
    if (tab) out.set("tab", tab);
  }
  const qs = out.toString();
  return `/v2/knowledge-bases${qs ? `?${qs}` : ""}`;
}

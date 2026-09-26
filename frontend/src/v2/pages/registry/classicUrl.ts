/**
 * Maps a classic `/registry` query string onto the native V2 page, so links from
 * other modules, bookmarks and hand-overs keep landing in the right sub-page.
 *
 * classic                              → V2
 * ?view=edit&record=<id>               → ?view=edit&id=<id>
 * ?record=<id>        (list selection) → ?view=detail&id=<id>
 * ?view=register[&type=MCP|AGENT_SKILLS], ?view=discoverable, ?view=a2a-demo → unchanged
 */
export function classicRegistryToV2(search: string): string {
  const params = new URLSearchParams(search);
  const view = params.get("view");
  const record = params.get("record");
  const out = new URLSearchParams();
  if (view === "edit" && record) {
    out.set("view", "edit");
    out.set("id", record);
  } else if (view === "register") {
    out.set("view", "register");
    const type = params.get("type");
    if (type === "MCP" || type === "AGENT_SKILLS") out.set("type", type);
  } else if (view === "discoverable" || view === "a2a-demo") {
    out.set("view", view);
  } else if (!view && record) {
    out.set("view", "detail");
    out.set("id", record);
  } else if (view === "detail" || view === "edit") {
    // already a V2-shaped link (`id=`) — pass it through
    const id = params.get("id");
    if (id) {
      out.set("view", view);
      out.set("id", id);
    }
  }
  const qs = out.toString();
  return `/v2/registry${qs ? `?${qs}` : ""}`;
}

/**
 * Maps a classic `/governance` query string onto the native V2 page, so links from
 * other modules, bookmarks and hand-overs keep landing in the right sub-page.
 *
 * classic `?view=`            → V2
 *   (none) | gateways         → /v2/governance
 *   tools                     → ?tab=tools
 *   gateway&gateway=G         → ?view=gateway&gateway=G
 *   decisions|audit&gateway=G → ?view=gateway&gateway=G&section=decisions|audit
 *   policy&gateway=G[&policy] → ?view=policy&gateway=G[&policy=P]
 * A Gateway-scoped view without `gateway` falls back to the inventory, as the
 * classic page asked the operator to choose a Gateway first.
 */
export function classicGovernanceToV2(search: string): string {
  const params = new URLSearchParams(search);
  const view = params.get("view");
  const gateway = params.get("gateway");
  const out = new URLSearchParams();
  if (view === "tools") {
    out.set("tab", "tools");
  } else if (gateway && (view === "gateway" || view === "decisions" || view === "audit")) {
    out.set("view", "gateway");
    out.set("gateway", gateway);
    if (view !== "gateway") out.set("section", view);
  } else if (gateway && view === "policy") {
    out.set("view", "policy");
    out.set("gateway", gateway);
    const policy = params.get("policy");
    if (policy) out.set("policy", policy);
  }
  const query = out.toString();
  return `/v2/governance${query ? `?${query}` : ""}`;
}

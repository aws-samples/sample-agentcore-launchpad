/**
 * Maps a classic `/governance` query string onto the native V2 page, so links from
 * other modules, bookmarks and hand-overs keep landing in the right sub-page.
 *
 * classic `?view=`         → V2
 *   (none) | gateways      → /v2/governance
 *   tools                  → ?tab=tools
 *   gateway&gateway=G      → ?view=gateway&gateway=G
 *   policies | decisions | audit | policy[&policy=P] | simulate
 *                          → /v2/policy (the Policy module is its own top-level page now, Global
 *                            structure; a policy id rides along as `?policy=P` and the page
 *                            resolves the engine that owns it)
 * A Gateway-scoped view without `gateway` falls back to the inventory, as the
 * classic page asked the operator to choose a Gateway first.
 */
import { POLICY_PATH } from "../policy/common";

const POLICY_VIEWS = ["policies", "decisions", "audit", "policy", "simulate"];

export function classicGovernanceToV2(search: string): string {
  const params = new URLSearchParams(search);
  const view = params.get("view") ?? params.get("tab");
  const gateway = params.get("gateway");
  if (view && POLICY_VIEWS.includes(view)) {
    const out = new URLSearchParams();
    const policy = params.get("policy");
    if (policy) out.set("policy", policy);
    if (view === "decisions") out.set("tab", "decisions");
    const query = out.toString();
    return `${POLICY_PATH}${query ? `?${query}` : ""}`;
  }
  const out = new URLSearchParams();
  if (view === "tools") out.set("tab", view);
  else if (gateway && view === "gateway") {
    out.set("view", "gateway");
    out.set("gateway", gateway);
    const section = params.get("section");
    if (section) out.set("section", section);
  }
  const query = out.toString();
  return `/v2/governance${query ? `?${query}` : ""}`;
}

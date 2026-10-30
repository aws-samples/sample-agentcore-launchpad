/**
 * `/v2/my-connections` was folded into the Connections page as its `?view=mine` tab
 * (R13, N13-obs5). Saved bookmarks and old links keep working: the query string rides
 * along, `view` is forced to `mine`.
 */
export function legacyMyConnectionsToV2(search: string): string {
  const params = new URLSearchParams(search);
  params.set("view", "mine");
  return `/v2/connections?${params.toString()}`;
}

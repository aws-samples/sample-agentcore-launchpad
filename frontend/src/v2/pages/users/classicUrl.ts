/**
 * Maps a classic `/users` query string onto the native V2 page, so links from
 * other modules, bookmarks and hand-overs keep landing in the right sub-page.
 *
 * The classic page only ever had list state (`?status=`, `?q=`, `?page=`), which
 * the V2 list reads under the same names; anything else is dropped.
 */
export function classicUsersToV2(search: string): string {
  const src = new URLSearchParams(search);
  const next = new URLSearchParams();
  for (const key of ["status", "q", "page"]) {
    const value = src.get(key);
    if (value) next.set(key, value);
  }
  const query = next.toString();
  return `/v2/users${query ? `?${query}` : ""}`;
}

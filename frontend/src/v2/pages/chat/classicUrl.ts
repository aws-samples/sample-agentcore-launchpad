/**
 * Maps a classic `/chat` query string onto the native V2 page, so links from
 * other modules, bookmarks and hand-overs keep landing in the right sub-page.
 * The V2 page reads the same deep-link params as the classic one —
 * `?agent=<id>` (preselect) and `&session=<id>` (replay + resume) — so the
 * query string carries over verbatim.
 */
export function classicChatToV2(search: string): string {
  return `/v2/chat${search}`;
}

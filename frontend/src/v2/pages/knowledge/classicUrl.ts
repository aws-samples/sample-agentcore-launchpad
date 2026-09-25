/**
 * Maps a classic `/knowledge-bases` query string onto the native V2 page, so links from
 * other modules, bookmarks and hand-overs keep landing in the right sub-page.
 */
export function classicKnowledgeBasesToV2(search: string): string {
  return `/v2/knowledge-bases${search}`;
}

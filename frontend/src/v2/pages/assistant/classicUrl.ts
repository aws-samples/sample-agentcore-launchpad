/**
 * Maps a classic `/create/assistant` query string onto the native V2 page, so links from
 * other modules, bookmarks and hand-overs keep landing in the right sub-page: the classic
 * `?conversation=<id>` deep link becomes `?view=detail&id=<id>`; anything else lands on
 * the conversation list.
 */
export function classicAssistantToV2(search: string): string {
  const conversation = new URLSearchParams(search).get("conversation");
  if (!conversation) return "/v2/assistant";
  return `/v2/assistant?${new URLSearchParams({ view: "detail", id: conversation }).toString()}`;
}

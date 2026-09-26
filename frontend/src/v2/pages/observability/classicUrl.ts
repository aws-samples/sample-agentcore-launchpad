/**
 * Maps a classic `/observability` query string onto the native V2 page, so links from
 * other modules, bookmarks and hand-overs keep landing in the right sub-page.
 *
 * Classic deep links: `?trace=<id>` (trace waterfall — governance decisions),
 * `?session=<id>` (session detail — chat, governance, run results), `?tab=`
 * (dashboard | sessions | traces). `?trace=` wins over `?session=`, as it did
 * on the classic page.
 */
export function classicObservabilityToV2(search: string): string {
  const src = new URLSearchParams(search);
  const out = new URLSearchParams();
  const trace = src.get("trace");
  const session = src.get("session");
  const tab = src.get("tab");
  const range = src.get("range");
  if (trace) {
    out.set("tab", "traces");
    out.set("view", "trace");
    out.set("id", trace);
  } else if (session) {
    out.set("tab", "sessions");
    out.set("view", "session");
    out.set("id", session);
  } else if (tab === "dashboard" || tab === "sessions" || tab === "traces") {
    out.set("tab", tab);
  }
  if (range) out.set("range", range);
  const qs = out.toString();
  return qs ? `/v2/observability?${qs}` : "/v2/observability";
}

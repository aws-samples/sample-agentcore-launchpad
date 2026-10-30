/**
 * Maps the classic `/settings/model-providers` URL onto the native V2 page (D21 §8).
 *
 * There is no classic page: the route only redirects. The sub-surface (`?view=`) and
 * the selected provider (`?provider=`) ride along so a bookmark or a hand-over from
 * an unavailable-model tooltip lands on the right card; anything else is dropped.
 */
export function classicModelProvidersToV2(search: string): string {
  const src = new URLSearchParams(search);
  const next = new URLSearchParams();
  for (const key of ["view", "provider"]) {
    const value = src.get(key);
    if (value) next.set(key, value);
  }
  const query = next.toString();
  return `/v2/settings/model-providers${query ? `?${query}` : ""}`;
}

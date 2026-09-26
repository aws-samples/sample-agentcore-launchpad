/**
 * `/agents/new` deep links in the V2 console. A link that carries a creation intent
 * — `method=zip_runtime|container|byoc|harness` (old V2 hand-off), `gateway=` or
 * `skill=` (Registry "use in a new agent") — lands on the native wizard with the
 * same prefill. A bare `/agents/new` is the classic page's system-presets surface
 * (install / configure platform presets, e.g. the assistant's "go to presets"),
 * which has no V2 twin yet: `null` ⇒ keep rendering the classic page.
 */
export function classicAgentNewToV2(search: string): string | null {
  const params = new URLSearchParams(search);
  const next = new URLSearchParams({ view: "new" });
  let intent = false;
  for (const key of ["method", "gateway", "skill"]) {
    const value = params.get(key);
    if (value) {
      next.set(key, value);
      intent = true;
    }
  }
  return intent ? `/v2/agents?${next.toString()}` : null;
}

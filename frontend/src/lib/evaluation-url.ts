// URL mapping between the classic `/evaluation?view=…` sub-pages and their V2 twins, plus
// the Experiments module's own redirect rule. Pure functions so the routing decisions are
// unit-testable without rendering `App`.
//
// N12-obs49 (user decision 2026-10-16) hid the configuration-bundle experiment (recommend →
// bundles → gateway/A-B → traffic → verdict → promote → cleanup) from both consoles;
// N13-obs2 (2026-10-17) deleted it, routes included. The Experiments module shows ONLY
// runtime canaries, and configuration deep links (`?mode=configuration`,
// `?view=new|detail&id=`, classic `exp=`) keep landing on the canary list.

/** The canary params a hand-off may carry (`canary=new|<id>`, the preselected champion). */
export const CANARY_PARAMS = ["canary", "champion"] as const;

/**
 * Where the V2 Experiments module sends a URL that still asks for the configuration
 * experiment, or `null` when the URL is a canary URL (list, `canary=new`, `canary=<id>`).
 * `mode=canary` is accepted but no longer required; `mode=configuration`, `view=` and
 * `id=` are configuration-only and redirect to the bare canary list.
 */
export function v2ExperimentsRedirect(search: string): string | null {
  const params = new URLSearchParams(search);
  const configurationOnly =
    params.get("mode") === "configuration" || params.has("view") || params.has("id");
  if (!configurationOnly) return null;
  return "/v2/eval/experiments";
}

/**
 * Where the classic `?view=experiment` sub-page sends a URL that is not already a canary
 * URL (`mode=canary`), or `null` when it is. Carries the canary param if one was given
 * alongside, drops the configuration `exp=`.
 */
export function classicExperimentRedirect(search: string): string | null {
  const params = new URLSearchParams(search);
  if (params.get("view") !== "experiment" || params.get("mode") === "canary") return null;
  const next = new URLSearchParams({ view: "experiment", mode: "canary" });
  const canary = params.get("canary");
  if (canary) next.set("canary", canary);
  return `/evaluation?${next.toString()}`;
}

/**
 * In V2 the classic evaluation page's new-run, experiment and online sub-pages have
 * native twins: their URLs (hand-offs from runs, agents, bookmarks) are mapped onto them.
 * Every other `/evaluation` view stays classic inside the V2 shell.
 */
export function v2EvaluationTarget(search: string): string | null {
  const params = new URLSearchParams(search);
  const view = params.get("view");
  const next = new URLSearchParams();
  if (view === "online") {
    const oe = params.get("oe");
    if (oe === "new") next.set("view", "new");
    else if (oe) {
      next.set("view", "detail");
      next.set("id", oe);
    }
    const q = next.toString();
    return `/v2/eval/online${q ? `?${q}` : ""}`;
  }
  // the new-run form: hand-offs prefill agent / dataset / evaluators
  if (view === "new") {
    next.set("view", "new");
    for (const key of ["agent", "dataset", "evaluators"]) {
      const value = params.get(key);
      if (value) next.set(key, value);
    }
    return `/v2/eval/tasks?${next.toString()}`;
  }
  // the experiments module is canary-only (N12-obs49): canary params carry over, the
  // configuration `exp=` deep link lands on the canary list
  if (view === "experiment") {
    for (const key of CANARY_PARAMS) {
      const value = params.get(key);
      if (value) next.set(key, value);
    }
    const q = next.toString();
    return `/v2/eval/experiments${q ? `?${q}` : ""}`;
  }
  return null;
}

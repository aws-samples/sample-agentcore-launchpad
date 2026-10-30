import { api, detached } from "./api";
import { setCustomEvaluatorNames } from "./evaluator-names";
import type { DerivedFrom } from "./scale-labels";

// The custom rows of the evaluator catalogue, fetched once per minute for every label on
// the page: a derived evaluator reads its labels through its base built-in (N17-obs428),
// and every custom evaluator is named by its name, not its id (N17-obs429).
const TTL_MS = 60_000;
let catalog: { at: number; rows: Promise<Record<string, DerivedFrom>> } | null = null;

export function customEvaluatorRows(): Promise<Record<string, DerivedFrom>> {
  if (!catalog || Date.now() - catalog.at > TTL_MS) {
    // shared by every label on every page: never the calling page's signal (N17-obs476)
    const rows = detached(() => api.v2Evaluators())
      .then((res) => {
        const custom = (res.evaluators ?? []).filter((e) => e.source === "custom");
        setCustomEvaluatorNames(Object.fromEntries(
          custom.filter((e) => e.name).map((e) => [e.id, String(e.name)])));
        return Object.fromEntries(custom.map((e) => [e.id, {
          base_evaluator_id: e.base_evaluator_id ?? null, scale_labels: e.scale_labels ?? [],
        }]));
      })
      .catch(() => ({}) as Record<string, DerivedFrom>);
    catalog = { at: Date.now(), rows };
  }
  return catalog.rows;
}

/** Test seam: forget the cached catalogue (and the custom evaluators' names). */
export function resetScaleLabelCatalog() {
  catalog = null;
  setCustomEvaluatorNames({});
}

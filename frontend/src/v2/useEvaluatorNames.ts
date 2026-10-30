import { useEffect, useSyncExternalStore } from "react";

import { evaluatorNamesVersion, subscribeEvaluatorNames } from "../lib/evaluator-names";
import { customEvaluatorRows } from "../lib/scale-label-catalog";

/** Load the custom evaluators' names (N17-obs429) and re-render once they arrive, so every
 *  `evaluatorLabel` the component renders shows a custom evaluator by its name. Returns the
 *  store's version (a dependency for memoised labels). */
export function useEvaluatorNames(): number {
  useEffect(() => {
    void customEvaluatorRows();
  }, []);
  return useSyncExternalStore(subscribeEvaluatorNames, evaluatorNamesVersion, evaluatorNamesVersion);
}

import { useTranslation } from "react-i18next";

import { evaluatorLabel } from "../lib/evaluators";
import { useEvaluatorNames } from "./useEvaluatorNames";

/** An evaluator as a result view names it (N17-obs429): a built-in by its localized name, a
 *  custom one by its catalogue name — the id is the tooltip; a deleted evaluator keeps its id. */
export function EvaluatorName({ id, className }: { id: string; className?: string }) {
  const { t } = useTranslation();
  useEvaluatorNames();
  return <span className={className} title={id}>{evaluatorLabel(t, id)}</span>;
}

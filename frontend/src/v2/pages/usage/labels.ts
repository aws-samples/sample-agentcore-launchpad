// Copy hooks of the budget surfaces: a `usage_events.source` and a limit's basis as
// people read them (My usage, the Budgets tab, the user detail card).
import { useTranslation } from "react-i18next";

import type { BudgetBasis } from "../../../lib/api";
import { sourceKey } from "../../../lib/budgets";

/** The label of a source; an id the console has no copy for shows as is. */
export function useSourceLabel() {
  const { t } = useTranslation();
  return (source: string) => t(sourceKey(source), { defaultValue: source });
}

/** The short label of a basis (people table, previews); `level` names the level. */
export function useBasisLabel() {
  const { t } = useTranslation();
  return (basis: BudgetBasis, level: string | null | undefined) =>
    t(`v2.budgets.basis.${basis}`, { level: level ?? "" });
}

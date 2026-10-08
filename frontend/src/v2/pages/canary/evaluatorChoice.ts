import type { TFunction } from "i18next";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type EvaluatorRow } from "../../../lib/api";
import { evaluatorLabel } from "../../../lib/evaluators";
import { ONLINE_EVAL_DEFAULT } from "../../../lib/experiments";
import { useLoad } from "../../hooks";

const GSR = "Builtin.GoalSuccessRate";

/** The id part of an evaluator id or ARN (`…:evaluator/<id>`). */
export const evaluatorIdOf = (idOrArn: string) => idOrArn.split("/").pop() ?? idOrArn;

/** A custom judge's name from the account listing, else the built-in's label. */
export function evaluatorName(t: TFunction, rows: EvaluatorRow[] | undefined, id: string): string {
  const row = rows?.find((e) => e.id === id);
  return row?.source === "custom" && row.name ? row.name : evaluatorLabel(t, id);
}

/** The primary the UI proposes: the first selected custom judge, else Goal success
 *  rate, else the first pick. */
export function defaultPrimary(selected: string[], rows: EvaluatorRow[]): string {
  const isCustom = (id: string) => {
    const row = rows.find((e) => e.id === id);
    return row ? row.source === "custom" : !id.startsWith("Builtin.") && !id.startsWith("ThirdParty.");
  };
  const custom = selected.find(isCustom);
  if (custom) return custom;
  return selected.includes(GSR) ? GSR : (selected[0] ?? "");
}

export interface CanaryEvaluatorChoice {
  rows: EvaluatorRow[];
  loading: boolean;
  error: string | null;
  reload: () => void;
  selected: string[];
  setSelected: (next: string[]) => void;
  primary: string;
  setPrimary: (id: string) => void;
  /** the create body's fields (`primary_evaluator` is always sent once something is picked) */
  body: { online_evaluators: string[]; primary_evaluator?: string };
  /** "A, B" in the operator's language */
  names: string;
  primaryName: string;
}

/** Evaluator selection state for a canary create form: the builtin pair by default,
 *  primary = the first selected custom judge (else Goal success rate) until the
 *  operator picks one. */
export function useCanaryEvaluators(): CanaryEvaluatorChoice {
  const { t } = useTranslation();
  const load = useLoad(() => api.v2Evaluators(), "evaluators");
  const rows = useMemo(() => load.data?.evaluators ?? [], [load.data]);
  const [selected, setSelected] = useState<string[]>(() => [...ONLINE_EVAL_DEFAULT]);
  const [picked, setPicked] = useState<string | null>(null);
  const primary = picked && selected.includes(picked) ? picked : defaultPrimary(selected, rows);
  const names = selected.map((id) => evaluatorName(t, rows, id)).join(t("v2.canary.eval.sep"));
  return {
    rows,
    loading: load.loading,
    error: load.error,
    reload: load.reload,
    selected,
    setSelected,
    primary,
    setPrimary: setPicked,
    body: { online_evaluators: selected, ...(primary ? { primary_evaluator: primary } : {}) },
    names,
    primaryName: primary ? evaluatorName(t, rows, primary) : "—",
  };
}

import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { customEvaluatorRows } from "../lib/scale-label-catalog";
import { type DerivedFrom, scaleLabelDisplay } from "../lib/scale-labels";

/** A result's scale label as the reader's language says it (N17-obs428): a built-in
 *  evaluator's label (or a derived one's, through its base) in zh, the stored label as the
 *  tooltip; custom labels verbatim. */
export function ScaleLabel({ evaluatorId, label }: { evaluatorId?: string | null; label: string }) {
  const { i18n } = useTranslation();
  const zh = String(i18n.language ?? "").toLowerCase().startsWith("zh");
  const custom = !!evaluatorId && !evaluatorId.startsWith("Builtin.");
  const [derived, setDerived] = useState<DerivedFrom | null>(null);
  useEffect(() => {
    if (!zh || !custom || !evaluatorId) return;
    let live = true;
    void customEvaluatorRows().then((rows) => { if (live) setDerived(rows[evaluatorId] ?? null); });
    return () => { live = false; };
  }, [zh, custom, evaluatorId]);
  const shown = scaleLabelDisplay(evaluatorId, label, i18n.language, derived);
  return shown.original ? <span title={shown.original}>{shown.text}</span> : <>{shown.text}</>;
}

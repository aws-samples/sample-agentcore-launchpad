import ZH from "./builtin-scale-labels.zh.json";

// N17-obs428: the display text of a built-in evaluator's scale label. Records keep the
// label the evaluator stored (Global's English words — "Very Helpful", "Partially
// Correct", "Yes"); a zh console shows the per-evaluator meaning instead ("非常有帮助",
// "部分正确", Refusal "Yes" → "已拒答") with the stored label as the tooltip. Display only:
// nothing here feeds scores, pass logic or filters. A custom evaluator DERIVED from a
// built-in (same scale, another judge model) reads through its base while its own scale
// labels (if any) are still the base's; custom judges' labels stay verbatim — their scale
// is the author's text. The table is checked against the backend's built-in
// scale definitions by backend/tests/evaluation/test_scale_label_display.py.
type Table = Record<string, Record<string, string>>;
const TABLE: Table = Object.fromEntries(
  Object.entries(ZH as Record<string, unknown>)
    .filter(([key, value]) => !key.startsWith("_") && typeof value === "object" && value)
    .map(([key, value]) => [key, value as Record<string, string>]),
);

function lookup(map: Record<string, string> | undefined, label: string): string | undefined {
  if (!map) return undefined;
  if (label in map) return map[label];
  const folded = label.toLowerCase();
  const hit = Object.keys(map).find((k) => k.toLowerCase() === folded);
  return hit === undefined ? undefined : map[hit];
}

export interface ScaleLabelDisplay {
  /** what to show */
  text: string;
  /** the stored label when `text` translates it, else null */
  original: string | null;
}

/** What the evaluator catalogue says about a custom row (`GET /api/eval/evaluators`). */
export interface DerivedFrom {
  base_evaluator_id?: string | null;
  scale_labels?: string[] | null;
}

/** The built-in whose table a custom row's labels read through (N17-obs428): a derived
 *  row's base, unless the row carries a scale of its own that is not the base's. */
export function displayBase(row: DerivedFrom | null | undefined): string | null {
  const base = row?.base_evaluator_id ?? "";
  if (!base.startsWith("Builtin.") || !TABLE[base]) return null;
  const own = (row?.scale_labels ?? []).filter((l) => l);
  if (own.some((l) => lookup(TABLE[base], l) === undefined && lookup(TABLE["*"], l) === undefined)) {
    return null; // the author edited the scale: their words, verbatim
  }
  return base;
}

/** `label` of evaluator `evaluatorId` for language `lang` (`i18n.language`); `derived`
 *  is the catalogue row of a custom evaluator (its base built-in, its own scale). */
export function scaleLabelDisplay(
  evaluatorId: string | null | undefined,
  label: string,
  lang: string | null | undefined,
  derived?: DerivedFrom | null,
): ScaleLabelDisplay {
  const id = evaluatorId?.startsWith("Builtin.") ? evaluatorId : displayBase(derived);
  if (!String(lang ?? "").toLowerCase().startsWith("zh") || !id) {
    return { text: label, original: null };
  }
  const text = lookup(TABLE[id], label) ?? lookup(TABLE["*"], label);
  return text === undefined || text === label ? { text: label, original: null } : { text, original: label };
}

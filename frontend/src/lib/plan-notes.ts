/**
 * Coded evaluation-plan notes (N17-obs308): the backend keeps the notes `draft_plan`
 * writes as English sentences in the plan content and adds `note_details`
 * (`backend/app/assistant/plan_notes.py`) — per evaluator key / recommendation index the
 * note verbatim (`message`) and its `{code, params, text}` parts. Known codes render in
 * the member's language; text the platform did not write (a seed's own note, a member
 * edit) and payloads from an older backend render verbatim.
 */
import type { TFunction } from "i18next";

import { gateLabel } from "./evalPlanGate";

export interface PlanNotePart {
  code: string;
  params: Record<string, unknown>;
  text: string;
}

export interface PlanNoteDetail {
  message: string;
  parts: PlanNotePart[];
}

export interface PlanNoteDetails {
  evaluators: Record<string, PlanNoteDetail>;
  recommendations: Record<string, PlanNoteDetail>;
}

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

function gateText(t: TFunction, p: Record<string, unknown>): string {
  const metric = typeof p.metric === "string" && p.metric ? ` · ${t("assistantEval.gateMetric", { m: p.metric })}` : "";
  return gateLabel(t, p.blocking === true, num(p.threshold)) + metric;
}

function sourceText(t: TFunction, source: unknown): string {
  return source === "stated" || source === "parsed" ? t(`assistantEval.gateSource.${source}`) : String(source ?? "");
}

function whyText(t: TFunction, p: Record<string, unknown>): string {
  const id = String(p.evaluator_id ?? "");
  const why =
    p.why_code === "not_listed_or_seed" ? t("assistantEval.note.whyNotListedOrSeed", { id })
      : p.why_code === "not_listed" ? t("assistantEval.note.whyNotListed", { id })
        : typeof p.why === "string" && p.why ? p.why : "";
  return why ? t("assistantEval.note.why", { why }) : "";
}

function partText(t: TFunction, part: PlanNotePart): string {
  const p = part.params ?? {};
  switch (part.code) {
    case "evaluation_plan.note.named_in_recommendation":
      return t("assistantEval.note.namedIn", { n: p.index });
    case "evaluation_plan.note.gate_from_recommendation":
      return t("assistantEval.note.gateFrom", { n: p.index, source: sourceText(t, p.source), gate: gateText(t, p) });
    case "evaluation_plan.note.renamed":
      return t("assistantEval.note.renamed", { name: String(p.name ?? "") });
    case "evaluation_plan.note.gate_carried":
      return t("assistantEval.note.carried", {
        gate: gateText(t, p), source: sourceText(t, p.source),
        targets: Array.isArray(p.targets) ? p.targets.map(String).join(", ") : "",
      });
    case "evaluation_plan.note.unresolved_gate":
      return t("assistantEval.note.unresolvedGate", { gate: gateText(t, p), source: sourceText(t, p.source), why: whyText(t, p) });
    case "evaluation_plan.note.unclassified":
      return t("assistantEval.note.unclassified", { why: whyText(t, p) });
    default:
      return part.text;
  }
}

/** The note in the member's language, or the note itself when no matching detail exists. */
export function localizedNote(t: TFunction, note: string, detail: PlanNoteDetail | null | undefined): string {
  if (!detail || detail.message !== note || !Array.isArray(detail.parts) || detail.parts.length === 0) return note;
  return detail.parts.map((part) => partText(t, part)).join(t("assistantEval.note.separator"));
}

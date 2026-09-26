import type { TFunction } from "i18next";

import { ApiError } from "./api";
import type { SkillLabTask, SkillLabTaskgenRowEdit } from "./api";

/**
 * One generated row under review. `index` is the row's position in the job's
 * immutable `generated_tasks.json` — the only thing the save request uses to
 * name a row. Only the four author fields are editable; everything else on
 * `original` (files, attachments, target skills) is rebuilt server-side.
 */
export interface TaskgenReviewDraft {
  index: number;
  original: SkillLabTask;
  id: string;
  question: string;
  rubric: string;
  taskType: string;
  excluded: boolean;
}

const str = (value: unknown): string => (typeof value === "string" ? value : "");

export const toReviewDrafts = (tasks: SkillLabTask[]): TaskgenReviewDraft[] =>
  tasks.map((task, index) => ({
    index,
    original: task,
    id: str(task.id),
    question: str(task.question),
    rubric: str(task.rubric),
    taskType: str(task.task_type),
    excluded: false,
  }));

export interface TaskgenReviewSelection {
  /** Rows that will be saved, in generated order. */
  kept: TaskgenReviewDraft[];
  /** True when any row is excluded or any author field differs from the original. */
  dirty: boolean;
  /**
   * Request payload: `undefined` when nothing changed (the legacy full import),
   * otherwise one entry per kept row carrying ONLY the fields that changed.
   */
  tasks: SkillLabTaskgenRowEdit[] | undefined;
}

/** Trimmed the way the taskset editor trims: ids and types, never prose. */
const rowEdit = (draft: TaskgenReviewDraft): SkillLabTaskgenRowEdit => {
  const edit: SkillLabTaskgenRowEdit = { index: draft.index };
  const id = draft.id.trim();
  if (id !== str(draft.original.id)) edit.id = id;
  if (draft.question !== str(draft.original.question)) edit.question = draft.question;
  if (draft.rubric !== str(draft.original.rubric)) edit.rubric = draft.rubric;
  const taskType = draft.taskType.trim();
  if (taskType !== str(draft.original.task_type)) edit.task_type = taskType;
  return edit;
};

export function reviewSelection(drafts: TaskgenReviewDraft[]): TaskgenReviewSelection {
  const kept = drafts.filter((draft) => !draft.excluded);
  const edits = kept.map(rowEdit);
  const dirty =
    kept.length !== drafts.length || edits.some((edit) => Object.keys(edit).length > 1);
  return { kept, dirty, tasks: dirty ? edits : undefined };
}

/**
 * Why the save button is disabled, or null when the selection can be sent.
 * Mirrors the server's cheap pre-checks (empty selection, blank required fields,
 * duplicate ids) so the reason is explained before a round trip; the server
 * remains authoritative and its errors are shown next to the button.
 */
export function reviewBlocker(
  drafts: TaskgenReviewDraft[],
  t: (key: string, options?: Record<string, unknown>) => string,
): string | null {
  const { kept } = reviewSelection(drafts);
  if (kept.length === 0) return t("skillLab.taskgen.review.noneKept");
  const incomplete = kept.find(
    (draft) => !draft.id.trim() || !draft.question.trim() || !draft.rubric.trim(),
  );
  if (incomplete) return t("skillLab.taskgen.review.rowIncomplete", { n: incomplete.index + 1 });
  const seen = new Set<string>();
  const duplicates = new Set<string>();
  for (const draft of kept) {
    const id = draft.id.trim();
    if (seen.has(id)) duplicates.add(id);
    seen.add(id);
  }
  if (duplicates.size > 0)
    return t("skillLab.taskgen.review.duplicateIds", { ids: [...duplicates].join(", ") });
  return null;
}

/**
 * Localize a refused save. The server's stable code selects the sentence and
 * its `detail` supplies the ids/row numbers, so the Chinese UI never falls back
 * to the English server message while keeping every identifier the message named.
 * Validator and request-validation refusals list their per-row / per-field
 * diagnostics (split, row, id, field, limit) as plain text lines. The draft and
 * selection are untouched by any of this — the caller only stores the error.
 */
export function describeSaveError(err: unknown, t: TFunction): string {
  if (!(err instanceof ApiError)) return String(err);
  // Helpers are nested on purpose: the function is self-contained so an external
  // probe can evaluate it alone (see self-evolution host probes).
  // Bounds for the diagnostics rendered from a refused save. Server detail is
  // untrusted text: it is coerced to plain strings, control characters stripped,
  // capped, and rendered as React text (never HTML). Anything left out is
  // disclosed as a count, never dropped silently.
  const MAX_ISSUE_LINES = 6;
  const MAX_ISSUE_CHARS = 240;

  function plainText(value: unknown, t: TFunction): string {
    const raw =
      typeof value === "string"
        ? value
        : typeof value === "number" || typeof value === "boolean"
          ? String(value)
          : "";
    // eslint-disable-next-line no-control-regex -- strip C0/C1 controls incl. newlines
    const clean = raw.replace(/[\u0000-\u001f\u007f-\u009f]+/g, " ").trim();
    return clean.length > MAX_ISSUE_CHARS
      ? `${clean.slice(0, MAX_ISSUE_CHARS)}… ${t("skillLab.taskgen.err.truncated")}`
      : clean;
  }

  /** `{split, message}` rows from the task validator (skill_lab.taskset_invalid). */
  function validatorIssueLine(item: unknown, t: TFunction): string | null {
    if (item === null || typeof item !== "object" || Array.isArray(item)) return null;
    const { split, message } = item as { split?: unknown; message?: unknown };
    const text = plainText(message, t);
    if (!text) return null;
    const where = plainText(split, t);
    return where ? `${where}: ${text}` : text;
  }

  /** Pydantic rows `{loc, msg, ctx}` from FastAPI (validation.invalid_request):
   *  `["body","tasks",0,"id"]` → "tasks #1 · id", plus any numeric limits in ctx. */
  function requestIssueLine(item: unknown, t: TFunction): string | null {
    if (item === null || typeof item !== "object" || Array.isArray(item)) return null;
    const { loc, msg, ctx } = item as { loc?: unknown; msg?: unknown; ctx?: unknown };
    const text = plainText(msg, t);
    if (!text) return null;
    const parts = (Array.isArray(loc) ? loc : []).filter((part) => part !== "body");
    const where = parts
      .map((part, i) =>
        typeof part === "number" && parts[i - 1] === "tasks" ? `#${part + 1}` : plainText(part, t),
      )
      .filter(Boolean)
      .join(" · ");
    const limits =
      ctx !== null && typeof ctx === "object" && !Array.isArray(ctx)
        ? Object.entries(ctx as Record<string, unknown>)
            .filter(([, v]) => typeof v === "number" || typeof v === "string")
            .map(([k, v]) => `${plainText(k, t)} ${plainText(v, t)}`)
            .join(", ")
        : "";
    return `${where ? `${where}: ` : ""}${text}${limits ? ` (${limits})` : ""}`;
  }

  /** Bounded, disclosed list: at most MAX_ISSUE_LINES lines; unreadable entries and
   *  the overflow are each reported as a count. */
  function issueLines(
    detail: unknown,
    toLine: (item: unknown, t: TFunction) => string | null,
    t: TFunction,
  ): string[] {
    const items = Array.isArray(detail) ? detail : detail === null || detail === undefined ? [] : [detail];
    const lines: string[] = [];
    let unreadable = 0;
    for (const item of items) {
      const line = toLine(item, t);
      if (line === null) unreadable += 1;
      else lines.push(`• ${line}`);
    }
    const shown = lines.slice(0, MAX_ISSUE_LINES);
    if (lines.length > shown.length)
      shown.push(t("skillLab.taskgen.err.moreIssues", { n: lines.length - shown.length }));
    if (unreadable > 0) shown.push(t("skillLab.taskgen.err.unreadableIssues", { n: unreadable }));
    return shown;
  }

  const detail = (err.detail ?? {}) as {
    ids?: unknown;
    reason?: string;
    index?: unknown;
    count?: unknown;
  };
  const ids = Array.isArray(detail.ids) ? detail.ids.map(String).join(", ") : "";
  switch (err.code) {
    case "skill_lab.taskgen_empty_selection":
      return t("skillLab.taskgen.review.noneKept");
    case "skill_lab.taskgen_duplicate_id":
      return t("skillLab.taskgen.review.duplicateIds", { ids: ids || err.message });
    case "skill_lab.expansion_conflict":
      return t("skillLab.taskgen.err.expansionConflict", { ids: ids || err.message });
    case "skill_lab.already_imported":
      return t("skillLab.taskgen.err.alreadySaved");
    case "skill_lab.taskgen_bad_selection":
      if (detail.reason === "out_of_range" || detail.reason === "repeated")
        return t(`skillLab.taskgen.err.badSelection.${detail.reason}`, {
          row: Number(detail.index) + 1,
          total: Number(detail.count),
        });
      return err.message;
    case "skill_lab.taskset_invalid": {
      const lines = issueLines(err.detail, validatorIssueLine, t);
      return [t("skillLab.taskgen.err.validatorRefused"), ...lines].join("\n");
    }
    case "validation.invalid_request": {
      const lines = issueLines(err.detail, requestIssueLine, t);
      return [t("skillLab.taskgen.err.requestRefused"), ...lines].join("\n");
    }
    default:
      return err.message;
  }
}

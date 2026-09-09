import type { SkillLabTask, SkillLabTaskgenRowEdit } from "../../lib/api";

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

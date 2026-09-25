import type { TFunction } from "i18next";

import type { SkillLabAssetDescriptor, SkillLabTask, SkillLabTasksetMode } from "./api";

/**
 * Task-set editor model shared by the classic and V2 Skill Lab pages: editable
 * drafts, the client-side mirror of the vendored validator, and the JSON upload
 * interpretation. Pure — no React state lives here.
 */

export const SINGLE_SPLIT = "tasks";
export const SPLIT_ORDER = ["train", "val", "test"] as const;

/** Splits a mode carries, in the order the backend expects them. */
export const splitsFor = (mode: SkillLabTasksetMode): string[] =>
  mode === "single" ? [SINGLE_SPLIT] : [...SPLIT_ORDER];

export interface TaskAssetDraft {
  key: string;
  path: string;
  value: SkillLabAssetDescriptor;
}

/**
 * One editable task row. `original` carries the stored object so unknown keys
 * (`files`, `judge_mode`, `artifact_checks`, anything the CLI grows later)
 * survive an edit untouched — only the four edited fields are overwritten.
 */
export interface TaskDraft {
  key: string;
  original: SkillLabTask | null;
  id: string;
  question: string;
  rubric: string;
  taskType: string;
  files: Record<string, string>;
  assets: TaskAssetDraft[];
  assetBusy: boolean;
  assetError: string | null;
}

export type Drafts = Record<string, TaskDraft[]>;

let draftSeq = 0;
export const draftKey = () => `d${++draftSeq}`;

export const taskId = (n: number) => `task_${String(n).padStart(3, "0")}`;

export const emptyDraft = (n: number): TaskDraft => ({
  key: draftKey(),
  original: null,
  id: taskId(n),
  question: "",
  rubric: "",
  taskType: "",
  files: {},
  assets: [],
  assetBusy: false,
  assetError: null,
});

/**
 * Starting rows for a layout. `test` starts empty on purpose: it is optional,
 * and a seeded blank row there would fail the required-field checks and block a
 * train/val-only save.
 */
export const seedDrafts = (mode: SkillLabTasksetMode): Drafts =>
  mode === "single"
    ? { [SINGLE_SPLIT]: [emptyDraft(1)] }
    : { train: [emptyDraft(1)], val: [emptyDraft(1)], test: [] };

const rawTaskFiles = (task: SkillLabTask): Record<string, string | SkillLabAssetDescriptor> => {
  if (task.files === null || typeof task.files !== "object" || Array.isArray(task.files)) return {};
  return task.files as Record<string, string | SkillLabAssetDescriptor>;
};

const taskTextFiles = (task: SkillLabTask): Record<string, string> =>
  Object.fromEntries(
    Object.entries(rawTaskFiles(task)).filter(
      (entry): entry is [string, string] => typeof entry[1] === "string",
    ),
  );

const taskAssetDrafts = (task: SkillLabTask): TaskAssetDraft[] =>
  Object.entries(rawTaskFiles(task)).flatMap(([path, value]) =>
    typeof value === "string" ? [] : [{ key: draftKey(), path, value: { ...value } }],
  );

export const toDraft = (task: SkillLabTask): TaskDraft => ({
  key: draftKey(),
  original: task,
  id: typeof task.id === "string" ? task.id : "",
  question: typeof task.question === "string" ? task.question : "",
  rubric: typeof task.rubric === "string" ? task.rubric : "",
  taskType: typeof task.task_type === "string" ? task.task_type : "",
  files: taskTextFiles(task),
  assets: taskAssetDrafts(task),
  assetBusy: false,
  assetError: null,
});

export function toTask(draft: TaskDraft): SkillLabTask {
  const task: SkillLabTask = {
    ...(draft.original ?? {}),
    id: draft.id.trim(),
    question: draft.question,
    rubric: draft.rubric,
  };
  const taskType = draft.taskType.trim();
  if (taskType) task.task_type = taskType;
  else delete task.task_type;
  const files: Record<string, string | SkillLabAssetDescriptor> = { ...draft.files };
  for (const asset of draft.assets) files[asset.path.trim()] = asset.value;
  if (Object.keys(files).length) task.files = files;
  else delete task.files;
  return task;
}

/** Next free `task_NNN` for the add-row button. */
export function suggestId(list: TaskDraft[]): string {
  let max = 0;
  for (const draft of list) {
    const m = /^task_(\d+)$/.exec(draft.id.trim());
    if (m) max = Math.max(max, Number(m[1]));
  }
  return taskId(Math.max(max + 1, list.length + 1));
}

export const isTaskArray = (value: unknown): value is SkillLabTask[] =>
  Array.isArray(value) && value.every((item) => item !== null && typeof item === "object");

export const countsLabel = (counts: Record<string, number>) =>
  Object.entries(counts)
    .map(([split, n]) => `${split} ${n}`)
    .join(" · ");

export const excerpt = (text: string, max = 90) =>
  text.length > max ? `${text.slice(0, max)}…` : text;

export const fileCount = (task: SkillLabTask): number => {
  const files = task.files;
  return files !== null && typeof files === "object" ? Object.keys(files).length : 0;
};

export const EXAMPLE_TASKS = `[
  {
    "id": "task_001",
    "question": "Summarize the attached earnings note in 5 bullets.",
    "rubric": "Passes when the summary has exactly 5 bullets and names revenue growth.",
    "task_type": "summarize",
    "files": { "input/note.md": "Q2 revenue grew 14% ..." }
  }
]`;

/** Accepted extensions for per-task input files and taskgen attachments. */
export const TASK_ASSET_ACCEPT = ".xlsx,.pdf,.png,.jpg,.jpeg,.webp,.md,.txt,.csv";

/** Client-side mirror of the vendored validator: per split, draft key → message. */
export function mirrorTaskErrors(drafts: Drafts, t: TFunction): Record<string, Record<string, string>> {
  const out: Record<string, Record<string, string>> = {};
  for (const [split, list] of Object.entries(drafts)) {
    const seen = new Set<string>();
    const errors: Record<string, string> = {};
    for (const draft of list) {
      const id = draft.id.trim();
      if (!id) errors[draft.key] = t("skillLab.tasksets.err.idRequired");
      else if (id.includes("/") || id.includes("\\") || id.includes(".."))
        errors[draft.key] = t("skillLab.tasksets.err.idUnsafe");
      else if (seen.has(id)) errors[draft.key] = t("skillLab.tasksets.err.idDuplicate", { id });
      else if (!draft.question.trim()) errors[draft.key] = t("skillLab.tasksets.err.questionRequired");
      else if (!draft.rubric.trim()) errors[draft.key] = t("skillLab.tasksets.err.rubricRequired");
      const paths = [...Object.keys(draft.files), ...draft.assets.map((asset) => asset.path.trim())];
      for (const path of paths) {
        const unsafe =
          !path ||
          path.startsWith("/") ||
          path.startsWith("\\") ||
          path.startsWith("~") ||
          path.includes("\\") ||
          path.split("/").some((part) => !part || part === "." || part === "..") ||
          [".agents", ".claude", ".codex", ".git", "task.md"].includes(
            path.split("/")[0]?.toLowerCase(),
          );
        if (unsafe) {
          errors[draft.key] = t("skillLab.tasksets.err.assetPathUnsafe", { path });
          break;
        }
      }
      // Case-fold collision protection is a binary descriptor constraint.
      // Legacy inline text maps may contain case-distinct paths and must keep
      // round-tripping exactly as the historical loader allowed.
      const foldedAssetPaths = new Set<string>();
      for (const asset of draft.assets) {
        const path = asset.path.trim();
        const folded = path.toLowerCase();
        if (foldedAssetPaths.has(folded)) {
          errors[draft.key] = t("skillLab.tasksets.err.assetPathDuplicate", { path });
          break;
        }
        foldedAssetPaths.add(folded);
      }

      if (id) seen.add(id);
    }
    out[split] = errors;
  }
  return out;
}

/** What an uploaded task JSON means for the editor. */
export type TasksetUpload =
  | { kind: "error"; message: string }
  /** a bare task array, loaded into one split */
  | { kind: "split"; split: string; drafts: TaskDraft[]; note: string }
  /** an object keyed by split; `mode` may differ from the current one (create only) */
  | { kind: "splits"; mode: SkillLabTasksetMode; loaded: Drafts; note: string };

export function interpretTasksetUpload(
  parsed: unknown,
  opts: { mode: SkillLabTasksetMode; uploadSplit: string; editing: boolean },
  t: TFunction,
): TasksetUpload {
  if (isTaskArray(parsed)) {
    const split = opts.mode === "single" ? SINGLE_SPLIT : opts.uploadSplit;
    return {
      kind: "split",
      split,
      drafts: parsed.map(toDraft),
      note: t("skillLab.tasksets.upload.loadedSplit", { split, count: parsed.length }),
    };
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { kind: "error", message: t("skillLab.tasksets.upload.badShape") };
  }
  const entries = Object.entries(parsed as Record<string, unknown>);
  const known = [SINGLE_SPLIT, ...SPLIT_ORDER] as string[];
  const unknown = entries.filter(([key]) => !known.includes(key)).map(([key]) => key);
  if (unknown.length > 0) {
    return {
      kind: "error",
      message: t("skillLab.tasksets.upload.unknownSplits", { splits: unknown.join(", ") }),
    };
  }
  const bad = entries.find(([, value]) => !isTaskArray(value));
  if (bad) return { kind: "error", message: t("skillLab.tasksets.upload.badSplit", { split: bad[0] }) };
  const nextMode: SkillLabTasksetMode = entries.some(([key]) => key === SINGLE_SPLIT)
    ? "single"
    : "split";
  if (nextMode === "single" && entries.length > 1) {
    return { kind: "error", message: t("skillLab.tasksets.upload.mixedSplits") };
  }
  // editing cannot change the mode — the backend refuses mismatched keys
  if (opts.editing && nextMode !== opts.mode) {
    return { kind: "error", message: t("skillLab.tasksets.upload.modeLocked", { mode: opts.mode }) };
  }
  const loaded = Object.fromEntries(
    entries.map(([split, value]) => [split, (value as SkillLabTask[]).map(toDraft)]),
  ) as Drafts;
  return {
    kind: "splits",
    mode: nextMode,
    loaded,
    note: t("skillLab.tasksets.upload.loaded", {
      summary: entries.map(([split, value]) => `${split} ${(value as unknown[]).length}`).join(" · "),
    }),
  };
}

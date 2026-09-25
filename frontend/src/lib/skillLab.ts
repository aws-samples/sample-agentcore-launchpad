import type {
  SkillLabJobInfo,
  SkillLabStatus,
  SkillLabTargetBackend,
  SkillLabTasksetInfo,
} from "./api";

/** Skill Lab rules shared by the V2 pages (mirrors of the classic wizards). */

/** Upstream studio's fixed backend labels — the values are API contract. */
export const BACKEND_LABELS: Record<SkillLabTargetBackend, string> = {
  claude_code_exec: "claude_code_exec — Claude Code CLI",
  codex_exec: "codex_exec — Codex CLI",
};

/**
 * Judge suggestions are Converse inference-profile ids (`us.`/`global.`
 * prefixed) — the bedrock_chat judge calls Converse directly, and bare model
 * ids like `openai.gpt-5.6-sol` are rejected with "use an inference profile".
 * The agentic judge routes by family: openai.* → host codex CLI, else claude.
 */
export const JUDGE_MODEL_SUGGESTIONS = [
  "us.openai.gpt-5.6-sol",
  "global.anthropic.claude-opus-5",
  "us.anthropic.claude-sonnet-5",
];

/** Mirrors backend runner.judge_exec_route's family test. */
export const isOpenAiJudge = (model: string) => /^((us|eu|apac|global)\.)?openai\./.test(model);

/** The platform's blank-model default for a backend ("" until status lands). */
export function modelDefault(status: SkillLabStatus | null, backend: SkillLabTargetBackend): string {
  if (status === null) return "";
  return backend === "codex_exec" ? status.default_codex_target_model : status.default_target_model;
}

export const backendsOf = (status: SkillLabStatus | null): SkillLabTargetBackend[] =>
  status?.target_backends?.length
    ? status.target_backends
    : (Object.keys(BACKEND_LABELS) as SkillLabTargetBackend[]);

/**
 * Agentic-judge readiness for the selected judge model. Two independent host
 * prerequisites have to hold: the sandbox launcher (shared) and the CLI this
 * judge model's route resolves to. auto/agentic still run when one is missing,
 * but artifact tasks fail closed — warn, don't hide.
 */
export function judgeReadiness(status: SkillLabStatus | null, judgeModel: string) {
  const routesToCodex = isOpenAiJudge(judgeModel);
  const codexMissing = routesToCodex && status?.judge_codex_ready === false;
  const claudeMissing = !routesToCodex && status?.judge_claude_ready === false;
  const ready = status?.agentic_judge_ready !== false && !codexMissing && !claudeMissing;
  const unreadyKey = codexMissing
    ? "skillLab.backend.judgeCodexUnready"
    : claudeMissing
      ? "skillLab.backend.judgeClaudeUnready"
      : "skillLab.backend.judgeModeUnready";
  return { ready, unreadyKey };
}

/** Submitting needs both the exec worker and the local interpreter. */
export const canSubmitJobs = (status: SkillLabStatus | null) =>
  status === null || (status.provisioned && status.venv_ready);

export const LIST_POLL_MS = 8000;
export const JOB_POLL_MS = 2500;

export const LIVE_STATUSES = ["queued", "running"];
export const RESUMABLE_STATUSES = ["interrupted", "failed"];

export const isLiveJob = (job: SkillLabJobInfo) => LIVE_STATUSES.includes(job.status);

/** Wall time since start ("—" before the job starts). */
export function elapsed(job: SkillLabJobInfo): string {
  if (!job.started_at) return "—";
  const end = job.finished_at ? new Date(job.finished_at) : new Date();
  const seconds = Math.max(0, (end.getTime() - new Date(job.started_at).getTime()) / 1000);
  return seconds < 90 ? `${seconds.toFixed(0)}s` : `${(seconds / 60).toFixed(1)}m`;
}

export const tasksetLabel = (job: SkillLabJobInfo) =>
  job.split ? `${job.taskset_name} · ${job.split}` : job.taskset_name;

/** Skill name(s) of a job (multi-skill taskgen sources carry `names`). */
export const jobSkillName = (job: SkillLabJobInfo) =>
  job.skill_source?.names?.join(", ") ?? job.skill_source?.name ?? "";

/** Evaluation split preference: a held-out split first. */
const SPLIT_PREFERENCE = ["test", "val", "train"] as const;

/** Splits a set actually carries, in run-preference order ([] for single mode). */
export const evalSplitsOf = (info: SkillLabTasksetInfo): string[] =>
  info.mode === "single" ? [] : SPLIT_PREFERENCE.filter((split) => (info.counts[split] ?? 0) > 0);

/** Vendored skilleval default (train.batch_size) — one optimizer step per batch. */
export const TRAIN_BATCH_SIZE = 4;

/** The loader's ratio split for a single-mode set, mirrored for the cost line. */
const SINGLE_RATIO = { train: 0.4, val: 0.3 };

/** Task counts a training run will actually touch — a single-mode set is auto-split. */
export function trainSplitCounts(info: SkillLabTasksetInfo | null): { train: number; val: number } {
  if (info === null) return { train: 0, val: 0 };
  if (info.mode === "single") {
    const total = info.counts.tasks ?? 0;
    return {
      train: Math.round(total * SINGLE_RATIO.train),
      val: Math.round(total * SINGLE_RATIO.val),
    };
  }
  return { train: info.counts.train ?? 0, val: info.counts.val ?? 0 };
}

/**
 * Approximate exec-worker task runs: one optimizer step per minibatch, so the
 * val split is re-scored more often than once an epoch; +2 covers the seed
 * baseline and the final test pass.
 */
export function trainRunEstimate(info: SkillLabTasksetInfo | null, epochs: number): number {
  const counts = trainSplitCounts(info);
  const steps = epochs * Math.max(1, Math.ceil(counts.train / TRAIN_BATCH_SIZE));
  return counts.train * epochs + counts.val * (steps + 2);
}

/** Gate verdict of a training step — the trainer writes composite action strings. */
export function stepVerdict(action: string | null): "accept" | "reject" | "skip" | "other" {
  const value = (action ?? "").toLowerCase();
  if (value.includes("accept")) return "accept";
  if (value.includes("reject")) return "reject";
  if (value.includes("skip")) return "skip";
  return "other";
}

/** Empty-ish extras (null, "", [], {}) must not open a detail view for nothing. */
export function extraText(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return null;
    return value.every((item) => typeof item === "string")
      ? (value as string[]).join("\n")
      : JSON.stringify(value, null, 2);
  }
  if (typeof value === "object") {
    if (Object.keys(value as object).length === 0) return null;
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

/** Save a blob under a file name (artifact downloads are fetched, never linked). */
export function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export const sizeLabel = (bytes: number) =>
  bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;

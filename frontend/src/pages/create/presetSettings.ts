/**
 * Pure helpers for editing a system preset's administrator-editable settings in
 * the shared agent editor (Create → CONFIGURE / Existing agents → EDIT).
 *
 * The maintenance route (`POST /api/system-agents/{key}/install`) takes a PARTIAL
 * edit, so the editor diffs the form against what is stored and posts only the
 * members that differ; the two optional knobs are `clear`ed (never silently
 * dropped) when the form empties them or the model no longer accepts a reasoning
 * effort. Nothing here reaches the network.
 */
import type {
  SystemPresetEditableField,
  SystemPresetInfo,
  SystemPresetInstallInput,
  SystemPresetSettings,
} from "../../lib/api";
import type { ModelSource, ReasoningEffort } from "../../lib/models";
import { supportsReasoningEffort } from "../../lib/models";

export const EFFORT_NONE = "none";
export type EffortChoice = ReasoningEffort | typeof EFFORT_NONE;
/** mirrors the backend `MAX_TOKENS_CEILING` (the server stays authoritative) */
export const MAX_TOKENS_CEILING = 131072;
/** `AgentSpec` loop defaults for an ordinary agent when the wizard creates one */
export const DEFAULT_MAX_ITERATIONS = 10;
export const DEFAULT_TIMEOUT_SECONDS = 300;

/** The editor's view of the editable members — strings where the input is free text. */
export interface PresetForm {
  model_source: ModelSource;
  model_id: string;
  /** empty string ⇒ no per-call ceiling (the knob is cleared) */
  max_tokens: string;
  reasoning_effort: EffortChoice;
  system_prompt: string;
  max_iterations: string;
  timeout_seconds: string;
  knowledge_bases: SystemPresetSettings["knowledge_bases"];
}

export function formFromSettings(settings: SystemPresetSettings): PresetForm {
  return {
    model_source: settings.model_source,
    model_id: settings.model_id,
    max_tokens: settings.max_tokens == null ? "" : String(settings.max_tokens),
    reasoning_effort: settings.reasoning_effort ?? EFFORT_NONE,
    system_prompt: settings.system_prompt,
    max_iterations: String(settings.max_iterations),
    timeout_seconds: String(settings.timeout_seconds),
    knowledge_bases: settings.knowledge_bases,
  };
}

/** `null` for an empty input, `NaN` for a non-integer, the integer otherwise. */
export function intOrNull(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  return /^\d+$/.test(trimmed) ? Number(trimmed) : Number.NaN;
}

/** The reasoning effort the form would actually send for its model (or null). */
export function effectiveEffort(form: Pick<PresetForm, "model_id" | "model_source" | "reasoning_effort">):
  ReasoningEffort | null {
  const allowed = supportsReasoningEffort(form.model_id.trim(), form.model_source);
  return allowed && form.reasoning_effort !== EFFORT_NONE ? form.reasoning_effort : null;
}

/**
 * The PARTIAL edit a save sends: only members that differ from what is stored, so
 * the server keeps everything else untouched. `{}` ⇔ nothing changed (the caller
 * decides whether that becomes a `force` re-publish).
 */
export function diffPresetSettings(
  form: PresetForm,
  stored: SystemPresetSettings,
): SystemPresetInstallInput {
  const body: SystemPresetInstallInput = {};
  const clear: ("max_tokens" | "reasoning_effort")[] = [];
  if (form.model_source !== stored.model_source) body.model_source = form.model_source;
  if (form.model_id.trim() !== stored.model_id) body.model_id = form.model_id.trim();
  const tokens = intOrNull(form.max_tokens);
  if (tokens === null) {
    if (stored.max_tokens != null) clear.push("max_tokens");
  } else if (tokens !== stored.max_tokens) {
    body.max_tokens = tokens;
  }
  const effort = effectiveEffort(form);
  if (effort === null) {
    if (stored.reasoning_effort != null) clear.push("reasoning_effort");
  } else if (effort !== stored.reasoning_effort) {
    body.reasoning_effort = effort;
  }
  if (form.system_prompt !== stored.system_prompt) body.system_prompt = form.system_prompt;
  const iterations = intOrNull(form.max_iterations);
  if (iterations !== null && iterations !== stored.max_iterations) body.max_iterations = iterations;
  const timeout = intOrNull(form.timeout_seconds);
  if (timeout !== null && timeout !== stored.timeout_seconds) body.timeout_seconds = timeout;
  const storedKbs = stored.knowledge_bases.map((kb) => kb.kb_id).sort().join(",");
  const formKbs = form.knowledge_bases.map((kb) => kb.kb_id).sort().join(",");
  if (storedKbs !== formKbs) body.knowledge_bases = form.knowledge_bases;
  if (clear.length) body.clear = clear;
  return body;
}

export type Translate = (key: string, opts?: Record<string, unknown>) => string;

/**
 * Client-side bounds for the inference / loop knobs (the server's are the same and
 * stay authoritative). Shared by the system-preset edit and the ordinary harness
 * form, so an invalid knob never leaves the browser on either path.
 */
export function knobProblems(
  form: Pick<PresetForm, "max_tokens" | "max_iterations" | "timeout_seconds">,
  t: Translate,
): string[] {
  const problems: string[] = [];
  const tokens = intOrNull(form.max_tokens);
  if (Number.isNaN(tokens) || (tokens !== null && (tokens < 1 || tokens > MAX_TOKENS_CEILING))) {
    problems.push(t("create.system.settings.errors.maxTokens", { max: MAX_TOKENS_CEILING }));
  }
  const iterations = intOrNull(form.max_iterations);
  if (iterations === null || Number.isNaN(iterations) || iterations < 1 || iterations > 100) {
    problems.push(t("create.system.settings.errors.maxIterations"));
  }
  const timeout = intOrNull(form.timeout_seconds);
  if (timeout === null || Number.isNaN(timeout) || timeout < 10 || timeout > 3600) {
    problems.push(t("create.system.settings.errors.timeout"));
  }
  return problems;
}

/** Whether one editable member of the form equals this build's default. */
export function isPresetDefault(
  key: SystemPresetEditableField,
  form: PresetForm,
  defaults: SystemPresetSettings,
): boolean {
  switch (key) {
    case "max_tokens":
      return intOrNull(form.max_tokens) === defaults.max_tokens;
    case "reasoning_effort":
      return (
        (form.reasoning_effort === EFFORT_NONE ? null : form.reasoning_effort) ===
        defaults.reasoning_effort
      );
    case "max_iterations":
      return intOrNull(form.max_iterations) === defaults.max_iterations;
    case "timeout_seconds":
      return intOrNull(form.timeout_seconds) === defaults.timeout_seconds;
    case "knowledge_bases":
      return form.knowledge_bases.length === 0;
    default:
      return form[key] === defaults[key];
  }
}

/** Human rows out of a 422 envelope (`detail.errors[]` or a bare list). */
export function apiErrorRows(detail: unknown): string[] {
  const rows = Array.isArray(detail)
    ? detail
    : Array.isArray((detail as { errors?: unknown[] } | null)?.errors)
      ? (detail as { errors: unknown[] }).errors
      : [];
  return rows
    .map((row) => {
      const r = row as { msg?: string; loc?: unknown[] };
      const loc = Array.isArray(r.loc) ? r.loc.filter((p) => p !== "body").join(".") : "";
      return [loc, r.msg].filter(Boolean).join(": ");
    })
    .filter(Boolean);
}

/** What the shared editor needs to open a preset: the row (stored settings,
 *  defaults, verdicts), the workspace it was read from, and whether the caller may
 *  save (administrator + settled + prerequisites) or only review. */
export interface PresetConfigureRequest {
  preset: SystemPresetInfo;
  workspaceId: string | null;
  editable: boolean;
  /** why the editor is read-only (rendered for non-editable callers) */
  readOnlyReason?: string;
}

/**
 * The editor hand-over for one preset row: editable only for an administrator whose
 * row is settled and whose workspace still meets the prerequisites (the server's
 * `can_configure`); everyone else reviews the same page read-only with the reason.
 * Shared by the panel's CONFIGURE button and the agent table's EDIT on a system row.
 */
export function presetConfigureRequest(
  preset: SystemPresetInfo,
  workspaceId: string | null,
  isAdmin: boolean,
  t: Translate,
): PresetConfigureRequest {
  const requirementText = preset.requirements
    .map((req) => t(`create.system.requirementCodes.${req.code}`, { defaultValue: req.message }))
    .join("; ");
  const readOnlyReason = !isAdmin
    ? t("create.system.settings.readOnly")
    : preset.requirements.length > 0
      ? t("create.system.requirementsInstalled", { list: requirementText })
      : preset.status === "deploying" || preset.status === "uninstalling"
        ? t("create.system.settings.busy", { status: t(`create.system.status.${preset.status}`) })
        : undefined;
  return { preset, workspaceId, editable: isAdmin && preset.can_configure, readOnlyReason };
}

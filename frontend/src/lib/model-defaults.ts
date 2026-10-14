/**
 * The platform's configured model per role (`GET /api/model-defaults`).
 *
 * Every model the platform pre-selects is a backend setting an administrator can
 * override on the Models page (`/v2/models`), so the console must not hard-code
 * which model a picker starts on. Fetched once per page load and shared
 * module-wide; a failed fetch falls back to {@link STATIC_MODEL_DEFAULTS} without
 * being cached, so a later mount retries.
 *
 * Pickers keep working while the defaults load: they seed the static value and
 * adopt the configured one when it arrives, unless the member already picked a
 * model (`adoptModelDefault`). A stored value (an evaluator, a run, a spec) is
 * never replaced.
 */
import { useEffect, useRef, useState } from "react";

import { api, type ModelRole, type ModelRoleSetting } from "./api";

export type ModelDefaults = Record<ModelRole, string>;

/** The backend's built-in defaults (`backend/app/core/config.py`) — the initial
 *  value and the fallback when the fetch fails. Keep them equal: the Studio
 *  fallback in particular is also the model the backend scopes the agent's IAM to. */
export const STATIC_MODEL_DEFAULTS: Readonly<ModelDefaults> = Object.freeze({
  agent_default_model_id: "global.openai.gpt-6-sol",
  claude_sdk_model_id: "global.anthropic.claude-sonnet-5",
  judge_model_id: "global.openai.gpt-6-sol",
  judge_fallback_model_id: "global.anthropic.claude-sonnet-5-5",
  assistant_model_id: "global.openai.gpt-6-sol",
  architect_model_id: "global.openai.gpt-6-astra",
  actor_model_id: "global.openai.gpt-6-luna",
  studio_fallback_model_id: "global.anthropic.claude-sonnet-5",
  codegen_model: "global.anthropic.claude-sonnet-5",
  prompt_opt_default_model_id: "global.anthropic.claude-opus-5",
  skill_lab_judge_model_id: "us.openai.gpt-6-sol",
  skill_lab_target_model_id: "global.anthropic.claude-opus-5-5",
  skill_lab_codex_target_model_id: "global.openai.gpt-6-sol",
});

/** Every role, in the backend registry's order. */
export const MODEL_ROLES = Object.keys(STATIC_MODEL_DEFAULTS) as ModelRole[];

let resolved: ModelDefaults | null = null;
let inflight: Promise<ModelDefaults> | null = null;
const listeners = new Set<(defaults: ModelDefaults) => void>();

/** A server answer over the static defaults: an unknown role is ignored, and a
 *  missing / blank / non-string value keeps the static one — except where the role
 *  allows empty (the judge fallback: `""` means none). */
export function normalizeModelDefaults(raw: unknown): ModelDefaults {
  const out: ModelDefaults = { ...STATIC_MODEL_DEFAULTS };
  const rows = (raw as { roles?: unknown } | null)?.roles;
  if (!Array.isArray(rows)) return out;
  for (const row of rows as Partial<ModelRoleSetting>[]) {
    const role = row?.role;
    if (!role || !(role in out) || typeof row.model_id !== "string") continue;
    const value = row.model_id.trim();
    if (value || row.allow_empty) out[role] = value;
  }
  return out;
}

/** The configured defaults (one request per page load; static values on error). */
export function loadModelDefaults(): Promise<ModelDefaults> {
  if (resolved) return Promise.resolve(resolved);
  inflight ??= api.modelDefaults().then(
    (raw) => (resolved = normalizeModelDefaults(raw)),
    () => {
      inflight = null; // not cached: the next mount retries
      return { ...STATIC_MODEL_DEFAULTS };
    },
  );
  return inflight;
}

/** Refetch after an administrator changed a role; mounted hooks get the new values. */
export async function reloadModelDefaults(): Promise<ModelDefaults> {
  resolved = null;
  inflight = null;
  const next = await loadModelDefaults();
  for (const listener of listeners) listener(next);
  return next;
}

/** The loaded defaults, or the static ones — for pure code that seeds a value. */
export function currentModelDefaults(): ModelDefaults {
  return resolved ?? STATIC_MODEL_DEFAULTS;
}

/** Test hook: forget the cached answer. */
export function resetModelDefaultsForTests(): void {
  resolved = null;
  inflight = null;
  listeners.clear();
}

/**
 * The model a picker pre-selects for `role`. `method: "container"` turns the agent
 * default into the Claude SDK default: the Claude Agent SDK drives Claude only, and
 * the backend gives a container spec without a model the same value.
 */
export function defaultModelForRole(
  role: ModelRole,
  opts: { method?: string; defaults?: ModelDefaults } = {},
): string {
  const defaults = opts.defaults ?? currentModelDefaults();
  const effective = role === "agent_default_model_id" && opts.method === "container" ? "claude_sdk_model_id" : role;
  return defaults[effective];
}

/** The configured defaults, or null while loading (already-loaded defaults are
 *  returned on the first render, so a later page mounts on the right value). */
export function useModelDefaults(): ModelDefaults | null {
  const [defaults, setDefaults] = useState<ModelDefaults | null>(resolved);
  useEffect(() => {
    listeners.add(setDefaults);
    return () => {
      listeners.delete(setDefaults);
    };
  }, []);
  useEffect(() => {
    if (defaults) return;
    let live = true;
    void loadModelDefaults().then((d) => {
      if (live) setDefaults(d);
    });
    return () => {
      live = false;
    };
  }, [defaults]);
  return defaults;
}

/**
 * `useModelDefaults` plus a one-shot `onArrive(defaults)` once they are available —
 * where a form that seeded the static value adopts the configured one. Runs on the
 * first commit when the defaults were already cached (a harmless re-adopt).
 */
export function useModelDefaultsArrival(onArrive: (defaults: ModelDefaults) => void): ModelDefaults | null {
  const defaults = useModelDefaults();
  const fired = useRef(false);
  const callback = useRef(onArrive);
  useEffect(() => {
    callback.current = onArrive;
  });
  useEffect(() => {
    if (!defaults || fired.current) return;
    fired.current = true;
    callback.current(defaults);
  }, [defaults]);
  return defaults;
}

/**
 * Whether a seeded field should switch to the configured default: only while it
 * still holds the value the form seeded (`seeded`). A stored value is never
 * `seeded`, and a member's own pick differs from it, so neither is replaced.
 */
export function adoptModelDefault(current: string, seeded: string, configured: string): string {
  return current === seeded && configured ? configured : current;
}

/** `options` with `current` prepended when it is not offered (a configured or
 *  stored id is never dropped from a closed list). */
export function withCurrentModel(options: readonly string[], current: string | null | undefined): string[] {
  const id = (current ?? "").trim();
  return !id || options.includes(id) ? [...options] : [id, ...options];
}

// ── role constraints (mirrors backend services/model_settings.validate) ─────

const GEO_PREFIXES = new Set(["global", "us", "eu", "apac", "jp", "au", "ca"]);
/** Bedrock models measured to refuse Converse toolConfig — never offered to a role that runs tools. */
const NO_TOOL_USE = /(^|\.)deepseek\.r1/;
/** The model-id shape the backend accepts (`MODEL_ID_RE`). */
export const MODEL_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:/-]{2,120}$/;

/** `anthropic` for `global.anthropic.x` / `anthropic.x`, and so on. */
export function modelFamily(modelId: string): string {
  const parts = modelId.split(".");
  if (parts.length >= 3 && GEO_PREFIXES.has(parts[0])) return parts[1];
  return parts.length >= 2 ? parts[0] : "";
}

/** Whether `role` accepts `modelId`: the id shape, the model family it can drive,
 *  and tool use where the role runs tools. `""` only where the role allows empty. */
export function roleAccepts(
  role: Pick<ModelRoleSetting, "constraint" | "allow_empty" | "needs_tools">,
  modelId: string,
): boolean {
  const id = modelId.trim();
  if (!id) return role.allow_empty;
  if (!MODEL_ID_RE.test(id)) return false;
  if (role.needs_tools && NO_TOOL_USE.test(id)) return false;
  return !role.constraint || role.constraint.includes(modelFamily(id));
}

/** The ids a role's picker offers: its effective model and its default first (always,
 *  so the current value is never dropped), then every catalog id the role accepts —
 *  blanks dropped, duplicates removed with the earliest position kept. */
export function roleModelOptions(
  role: Pick<ModelRoleSetting, "model_id" | "default" | "constraint" | "allow_empty" | "needs_tools">,
  catalog: readonly string[],
): string[] {
  const out: string[] = [];
  const add = (id: string, check: boolean) => {
    const trimmed = id.trim();
    if (trimmed && !out.includes(trimmed) && (!check || roleAccepts(role, trimmed))) out.push(trimmed);
  };
  add(role.model_id, false);
  add(role.default, false);
  for (const id of catalog) add(id, true);
  return out;
}

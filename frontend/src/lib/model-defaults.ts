/**
 * The server's configured model defaults (`GET /api/models/defaults`).
 *
 * A customer account may not have OpenAI or Anthropic models on Bedrock, so the
 * operator can change every platform default through env — the console must not
 * hard-code which model a picker pre-selects. Fetched once per page load and
 * shared module-wide (like `regions.ts`'s active region); a failed fetch falls
 * back to {@link STATIC_MODEL_DEFAULTS} without being cached, so a later mount
 * retries.
 *
 * Pickers keep working while the defaults load: they start on the static value
 * and adopt the configured one when it arrives, unless the user already picked a
 * model or the value came from a stored evaluator / run (`adoptModelDefault`).
 */
import { useEffect, useRef, useState } from "react";

import { api, type ModelDefaults } from "./api";

/** GLM-5.3's global profile: always offered as a non-OpenAI, non-Anthropic choice. */
export const GLM_MODEL_ID = "global.zai.glm-5.3";

/** Today's backend defaults (`backend/app/core/config.py`) — the initial value and the fallback when the fetch fails. */
export const STATIC_MODEL_DEFAULTS: Readonly<ModelDefaults> = Object.freeze({
  platform_model_id: "global.anthropic.claude-sonnet-5-5",
  judge_model_id: "global.openai.gpt-6.1-sol",
  judge_fallback_model_id: "global.anthropic.claude-sonnet-5-5",
  assistant_model_id: "global.openai.gpt-6.1-sol",
  architect_model_id: "global.openai.gpt-6-astra",
  actor_model_id: "global.openai.gpt-6-luna",
  studio_fallback_model_id: "global.anthropic.claude-sonnet-5-5",
  claude_sdk_model_id: "global.anthropic.claude-sonnet-5-5",
  agent_default_model_id: "global.moonshotai.kimi-k3",
  codegen_model: "global.anthropic.claude-sonnet-5-5",
  prompt_opt_default_model_id: "global.anthropic.claude-opus-5-5",
  skill_lab_judge_model_id: "global.openai.gpt-6.1-sol",
  skill_lab_target_model_id: "global.anthropic.claude-opus-5-5",
  skill_lab_codex_target_model_id: "global.openai.gpt-6.1-sol",
});

let resolved: ModelDefaults | null = null;
let inflight: Promise<ModelDefaults> | null = null;
const listeners = new Set<(defaults: ModelDefaults) => void>();

/** A server answer over the static defaults: a missing / blank / non-string key keeps
 *  the static value, except `judge_fallback_model_id`, where `""` means disabled. */
export function normalizeModelDefaults(raw: unknown): ModelDefaults {
  const out: ModelDefaults = { ...STATIC_MODEL_DEFAULTS };
  if (!raw || typeof raw !== "object") return out;
  const body = raw as Record<string, unknown>;
  for (const key of Object.keys(out) as (keyof ModelDefaults)[]) {
    const value = body[key];
    if (typeof value !== "string") continue;
    const trimmed = value.trim();
    if (trimmed || key === "judge_fallback_model_id") out[key] = trimmed;
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

/** Refetch after an administrator changed a model role; mounted hooks get the new values. */
export async function reloadModelDefaults(): Promise<ModelDefaults> {
  resolved = null;
  inflight = null;
  const next = await loadModelDefaults();
  for (const listener of listeners) listener(next);
  return next;
}

/** The loaded defaults, or null while none have arrived. */
export function peekModelDefaults(): ModelDefaults | null {
  return resolved;
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
 * still holds the value the form seeded (`seeded`) and the user has not touched it.
 * A stored evaluator's / run's value is never `seeded`, so it is never replaced.
 */
export function adoptModelDefault(current: string, seeded: string, configured: string, touched: boolean): string {
  return !touched && current === seeded && configured ? configured : current;
}

/**
 * A picker's option list: `first` (the configured default) leads, then `extras`
 * (configured fallbacks, GLM-5.3), then `base` (the static catalog) — blanks
 * dropped, duplicates removed with the earliest position kept.
 */
export function mergeModelOptions(
  first: string,
  extras: readonly (string | null | undefined)[],
  base: readonly string[],
): string[] {
  const out: string[] = [];
  for (const id of [first, ...extras, ...base]) {
    const trimmed = (id ?? "").trim();
    if (trimmed && !out.includes(trimmed)) out.push(trimmed);
  }
  return out;
}

/** `options` with `current` prepended when it is not offered (a stored id is never dropped). */
export function withCurrentModel(options: readonly string[], current: string | null | undefined): string[] {
  const id = (current ?? "").trim();
  return !id || options.includes(id) ? [...options] : [id, ...options];
}

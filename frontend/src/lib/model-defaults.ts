/**
 * The platform's configured model per role (`GET /api/model-defaults`, r15-e — the useful
 * subset of upstream #275).
 *
 * Every model the platform pre-selects is a backend setting an administrator can override
 * on Settings → Model Providers → *Model roles* (`/v2/settings/model-providers?view=roles`),
 * so the console must not hard-code which model a picker starts on. Fetched once per page
 * load and shared module-wide; a failed fetch falls back to {@link STATIC_MODEL_DEFAULTS}
 * without being cached, so a later mount retries.
 *
 * Pickers keep working while the defaults load: they seed the static value and adopt the
 * configured one when it arrives, unless the member already picked a model
 * (`adoptModelDefault`). A stored value (an evaluator, a run, a spec) is never replaced.
 */
import { useEffect, useRef, useState } from "react";

import { api, detached, type ModelRole, type ModelRoleSetting } from "./api";

export type ModelDefaults = Record<ModelRole, string>;

/** The backend's built-in defaults (`backend/app/core/config.py`) — the initial value and the
 *  fallback when the fetch fails. GLM 5.3 everywhere, Kimi K3 for the Skill Lab target
 *  (today's config, unchanged by r15-e); the judge fallback is "none". */
export const STATIC_MODEL_DEFAULTS: Readonly<ModelDefaults> = Object.freeze({
  agent_default: "global.zai.glm-5.3",
  judge: "global.zai.glm-5.3",
  judge_fallback: "",
  // N17-obs449: the insights failure detector (GLM-5.3's reasoning overran its budget)
  failure_detection: "global.moonshotai.kimi-k3",
  actor: "global.zai.glm-5.3",
  prompt_opt: "global.zai.glm-5.3",
  skill_lab_judge: "global.zai.glm-5.3",
  skill_lab_target: "global.moonshotai.kimi-k3",
  // N17-obs252
  memory_extract: "global.zai.glm-5.3",
  codegen: "global.moonshotai.kimi-k3",
  embedding: "amazon.titan-embed-text-v2:0",
});

/** Every role, in the backend registry's order (`services/model_roles.ROLES`). */
export const MODEL_ROLES = Object.keys(STATIC_MODEL_DEFAULTS) as ModelRole[];

let resolved: ModelDefaults | null = null;
let inflight: Promise<ModelDefaults> | null = null;
const listeners = new Set<(defaults: ModelDefaults) => void>();

/** A server answer over the static defaults: an unknown role is ignored, and a missing /
 *  blank / non-string value keeps the static one — except where the role allows empty (the
 *  judge fallback: `""` means none). */
export function normalizeModelDefaults(raw: unknown): ModelDefaults {
  const out: ModelDefaults = { ...STATIC_MODEL_DEFAULTS };
  const rows = (raw as { roles?: unknown } | null)?.roles;
  if (!Array.isArray(rows)) return out;
  for (const row of rows as Partial<ModelRoleSetting>[]) {
    const role = row?.role;
    if (!role || !(role in out) || typeof row.model_id !== "string") continue;
    const value = row.model_id.trim();
    if (value || row.allow_empty) out[role as ModelRole] = value;
  }
  return out;
}

/** The configured defaults (one request per page load; static values on error). */
export function loadModelDefaults(): Promise<ModelDefaults> {
  if (resolved) return Promise.resolve(resolved);
  // shared by every caller: never one page's signal (N17-obs476)
  inflight ??= detached(() => api.modelDefaults()).then(
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

/** Test hook: forget the cached answer (or plant one). */
export function resetModelDefaultsForTests(planted?: Partial<ModelDefaults>): void {
  resolved = planted ? { ...STATIC_MODEL_DEFAULTS, ...planted } : null;
  inflight = null;
  listeners.clear();
}

/** The configured defaults, or null while loading (already-loaded defaults are returned on
 *  the first render, so a later page mounts on the right value). */
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
 * `useModelDefaults` plus a one-shot `onArrive(defaults)` once they are available — where a
 * form that seeded the static value adopts the configured one. Runs on the first commit when
 * the defaults were already cached (a harmless re-adopt).
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
 * Whether a seeded field should switch to the configured default: only while it still holds
 * the value the form seeded (`seeded`). A stored value is never `seeded`, and a member's own
 * pick differs from it, so neither is replaced.
 */
export function adoptModelDefault(current: string, seeded: string, configured: string): string {
  return current === seeded && configured ? configured : current;
}

/** `options` with `current` prepended when it is not offered (a configured or stored id is
 *  never dropped from a closed list). */
export function withCurrentModel(options: readonly string[], current: string | null | undefined): string[] {
  const id = (current ?? "").trim();
  return !id || options.includes(id) ? [...options] : [id, ...options];
}

/** One enabled chat row of the registry page, as the roles tab sees it. */
export interface RoleCatalogRow {
  model_id: string;
  label: string;
  kind: "chat" | "embedding";
  enabled: boolean;
  tools: boolean;
  /** the row's provider is enabled (a disabled provider disables its rows — D21) */
  provider_enabled: boolean;
}

/** Whether `role` accepts `row` — mirrors `services/model_roles.validate`: an enabled row
 *  (model + provider) of the role's kind (chat; the `embedding` role takes embedding rows,
 *  N17-obs252) with tool use where the role runs tools. */
export function roleAcceptsRow(role: Pick<ModelRoleSetting, "needs_tools"> & { kind?: "chat" | "embedding" }, row: RoleCatalogRow): boolean {
  if (row.kind !== (role.kind ?? "chat") || !row.enabled || !row.provider_enabled) return false;
  return !role.needs_tools || row.tools;
}

/** The ids a role's picker offers: its effective model and its default first (always, so the
 *  current value is never dropped), then every catalog row the role accepts — blanks dropped,
 *  duplicates removed with the earliest position kept. */
export function roleModelOptions(
  role: Pick<ModelRoleSetting, "model_id" | "default" | "needs_tools" | "kind">,
  catalog: readonly RoleCatalogRow[],
): string[] {
  const out: string[] = [];
  const add = (id: string) => {
    const trimmed = id.trim();
    if (trimmed && !out.includes(trimmed)) out.push(trimmed);
  };
  add(role.model_id);
  add(role.default);
  for (const row of catalog) if (roleAcceptsRow(role, row)) add(row.model_id);
  return out;
}

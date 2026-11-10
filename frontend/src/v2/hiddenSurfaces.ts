import type { Feature } from "../lib/capabilities";

/**
 * The console's hidden surfaces: everything this build hides while the backend keeps serving it.
 * One decision ("not shown in this console build"), two shapes, one file. This is NOT
 * `Settings.features` / `/api/capabilities.features` — those un-mount backend routers; nothing
 * here changes what the server serves.
 *
 * * `features` — whole features dropped from the capabilities the server reports, so their
 *   navigation entry, home rows and routes behave exactly as if the deployment did not expose
 *   them. Operator decision 2026-10-20: the Skill Lab page is hidden until phase 2.
 * * `createEntries` — creation entry points next to the native wizard ("other ways"). A hidden
 *   entry stays routable (deep links keep working) but is not offered in the wizard's "other
 *   ways" card or the Agents page header. 2026-10-20 operator request: the Strands Studio canvas
 *   and "import existing Runtime" are hidden; the architect assistant and the system presets
 *   stay. `classicMethods` is the classic creation-method chooser on /agents/new (the
 *   system-presets page): every creation intent already lands in the V2 wizard, so the bare page
 *   is presets-only (user ruling 2026-10-21).
 *
 * The Policy module used to be gated here (`governanceTabs`, N17-obs170 / N17-obs497: the
 * policies / decisions / audit tabs under Gateway followed the workspace's live posture). With
 * the Global structure (2026-10-28) it is its own top-level page, `/v2/policy`, shown whenever
 * the `policy` feature is on — exactly like Global's console. No posture gate remains.
 *
 * `backend/tests/consistency/test_hidden_role_features.py` reads this file as text: the server's
 * `Settings.hidden_role_features` must equal `features` plus `studio` while
 * `createEntries.studio` is false (N17-obs252). Keep the `features: [...]` and `studio: <bool>,`
 * literals on their own lines.
 */
export const HIDDEN_SURFACES = {
  features: ["skill_lab"] as readonly Feature[],
  createEntries: {
    assistant: true,
    studio: false,
    import: false,
    presets: true,
    classicMethods: false,
  },
} as const;

export type CreateEntry = keyof typeof HIDDEN_SURFACES.createEntries;

export function featureHidden(feature: Feature): boolean {
  return HIDDEN_SURFACES.features.includes(feature);
}

export function entryShown(entry: CreateEntry): boolean {
  return HIDDEN_SURFACES.createEntries[entry];
}

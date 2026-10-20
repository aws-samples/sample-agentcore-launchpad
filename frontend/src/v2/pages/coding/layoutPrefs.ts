// The IDE layout (which side panels are collapsed, their widths) remembered per
// browser — one layout for every project, unlike the per-project composer choices in
// `prefs.ts`. Storage is a preference, never required: every access is guarded, and a
// stored value that is not a sane layout falls back to the default field by field.

import { DEFAULT_LAYOUT, FILES, type LayoutPrefs, STORED_MAX, WORKBENCH } from "./layout";

export const LAYOUT_KEY = "launchpad_coding_layout";

function storage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}

function width(value: unknown, min: number, max: number): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  const rounded = Math.round(value);
  return rounded >= min && rounded <= max ? rounded : null;
}

/** A stored value as layout preferences: anything unusable reads as the default. */
export function parseLayoutPrefs(raw: unknown): LayoutPrefs {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return { ...DEFAULT_LAYOUT };
  const value = raw as Record<string, unknown>;
  return {
    filesCollapsed: typeof value.filesCollapsed === "boolean" ? value.filesCollapsed : DEFAULT_LAYOUT.filesCollapsed,
    filesWidth: width(value.filesWidth, FILES.min, FILES.max),
    workbenchCollapsed:
      typeof value.workbenchCollapsed === "boolean" ? value.workbenchCollapsed : DEFAULT_LAYOUT.workbenchCollapsed,
    workbenchWidth: width(value.workbenchWidth, WORKBENCH.min, STORED_MAX),
  };
}

export function loadLayoutPrefs(): LayoutPrefs {
  try {
    const raw = storage()?.getItem(LAYOUT_KEY);
    return raw ? parseLayoutPrefs(JSON.parse(raw)) : { ...DEFAULT_LAYOUT };
  } catch {
    return { ...DEFAULT_LAYOUT };
  }
}

export function saveLayoutPrefs(prefs: LayoutPrefs): void {
  try {
    storage()?.setItem(LAYOUT_KEY, JSON.stringify(parseLayoutPrefs(prefs)));
  } catch {
    /* storage unavailable or full — the layout lasts until the page closes */
  }
}

// Composer choices (model, reasoning effort) remembered per project in this browser,
// and the deploy-name default. Storage is a preference, never required: every access
// is guarded, and a stored value the runtime no longer offers falls back to its default.

const KEY = "launchpad_coding_prefs";

export interface ComposerPrefs {
  model: string;
  effort: string;
}

function storage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}

export function loadPrefs(projectId: string): Partial<ComposerPrefs> {
  try {
    const raw = storage()?.getItem(`${KEY}:${projectId}`);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Partial<ComposerPrefs>;
    return {
      model: typeof parsed.model === "string" ? parsed.model : undefined,
      effort: typeof parsed.effort === "string" ? parsed.effort : undefined,
    };
  } catch {
    return {};
  }
}

export function savePrefs(projectId: string, prefs: ComposerPrefs): void {
  try {
    storage()?.setItem(`${KEY}:${projectId}`, JSON.stringify(prefs));
  } catch {
    /* storage unavailable — the choice lasts until the page closes */
  }
}

/** The value to select: the stored one if offered, else the default if offered, else the first. */
export function pickOffered(stored: string | null | undefined, fallback: string, offered: readonly string[]): string {
  if (stored && offered.includes(stored)) return stored;
  if (offered.includes(fallback) || offered.length === 0) return fallback;
  return offered[0];
}

/** A default Agent name for a project (`AGENT_NAME_RE`: `^[a-z][a-z0-9-]{2,47}$`). */
export function agentNameFrom(projectName: string): string {
  let slug = projectName
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .replace(/-{2,}/g, "-");
  slug = slug.replace(/^[^a-z]+/, "");
  if (slug.length < 3) slug = `coding-${slug || "agent"}`.replace(/-$/, "");
  return slug.slice(0, 48).replace(/-+$/, "");
}

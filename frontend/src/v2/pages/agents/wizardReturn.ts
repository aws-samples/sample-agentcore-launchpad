import type { UiVersion } from "../../../lib/ui-version";

/**
 * Where the wizard lands once a publish starts (v3-agent-management §4): in V3
 * the V3 detail page of the agent, otherwise null — the V2 page switches to its
 * own `view=detail` as before. `project` (see `projectSwitchFor`) rides along as
 * `?project=`, which the project provider consumes to switch the console there.
 */
export function wizardDoneTarget(version: UiVersion, agentId: string, project: string | null = null): string | null {
  if (version !== "v3") return null;
  const target = `/v3/agents?id=${encodeURIComponent(agentId)}`;
  return project ? `${target}&project=${encodeURIComponent(project)}` : target;
}

/**
 * The project a new agent's landing page must switch to: the one it was filed under
 * when that is not the console's current project — otherwise the detail page reads
 * the agent in the current project and answers "not found". Null when no switch is
 * needed (same project, or a re-publish, which keeps the agent's own project).
 */
export function projectSwitchFor(filedUnder: string | null | undefined, current: string | null | undefined): string | null {
  return filedUnder && filedUnder !== current ? filedUnder : null;
}

/**
 * The wizard's back button in V3: the page that started it — one step back in
 * history when the wizard was opened from inside the app (the launch page, the
 * agent's detail page, a template) — or, opened straight from a link, the V3
 * page it would have come from: the agent's detail for an edit, the launch page
 * for a new agent. Null outside V3: the V2 page keeps its own back.
 */
export function wizardBackTarget(
  version: UiVersion,
  locationKey: string,
  editId: string | undefined,
): -1 | string | null {
  if (version !== "v3") return null;
  // react-router's key of the first entry of a session is "default"
  if (locationKey !== "default") return -1;
  return editId ? `/v3/agents?id=${encodeURIComponent(editId)}` : "/v3/create";
}

/**
 * A new agent's picks after a project switch, kept to what the new project's catalogs
 * list: another project's KBs and Skills are not the agent's to mount. Skills the
 * wizard uploaded itself (`skillPaths` includes them) stay.
 */
export function pruneToCatalogs<F extends { selectedKbs: string[]; skills: string[] }>(
  form: F,
  catalog: { kbIds: readonly string[]; skillPaths: readonly string[] },
): Pick<F, "selectedKbs" | "skills"> {
  const kbs = new Set(catalog.kbIds);
  const skills = new Set(catalog.skillPaths);
  return {
    selectedKbs: form.selectedKbs.filter((id) => kbs.has(id)),
    skills: form.skills.filter((path) => skills.has(path)),
  };
}

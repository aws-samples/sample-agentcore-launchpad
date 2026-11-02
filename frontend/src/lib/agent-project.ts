import type { EligibleAgentProject } from "./api";

/**
 * The project the create form starts on: the shell's current project when the
 * server listed it as eligible (active, with a department, and the caller may create
 * agents in it), otherwise none — the member picks one. A workspace `default`
 * project (listed while the agent-project switch is on) is never preselected: filing
 * an agent outside every department is a deliberate pick.
 */
export function defaultAgentProjectId(
  eligible: readonly Pick<EligibleAgentProject, "id" | "is_default">[],
  currentId: string | null | undefined,
): string {
  return currentId && eligible.some((p) => p.id === currentId && !p.is_default) ? currentId : "";
}

/** The picked project, if it is still one the server lists (a stale pick reads as none). */
export function pickedAgentProjectId(
  eligible: readonly Pick<EligibleAgentProject, "id" | "is_default">[],
  picked: string | null,
  currentId: string | null | undefined,
): string {
  if (picked != null) return eligible.some((p) => p.id === picked) ? picked : "";
  return defaultAgentProjectId(eligible, currentId);
}

/** `Department · Project` — the label the picker shows. */
export function agentProjectLabel(p: Pick<EligibleAgentProject, "name" | "department_name">): string {
  return p.department_name ? `${p.department_name} · ${p.name}` : p.name;
}

/** The picker's option label: a workspace `default` project carries `defaultTag`
 *  (it belongs to no department), a business project reads `Department · Project`. */
export function agentProjectOptionLabel(
  p: Pick<EligibleAgentProject, "name" | "department_name" | "is_default">,
  defaultTag: string,
): string {
  return p.is_default ? `${p.name} · ${defaultTag}` : agentProjectLabel(p);
}

/** The i18n keys of the picker's hint and empty-list copy, by the agent-project switch. */
export function agentProjectCopyKeys(defaultAllowed: boolean | undefined): { hint: string; none: string } {
  return defaultAllowed
    ? { hint: "v3.create.project.hintDefault", none: "v3.create.project.noneDefault" }
    : { hint: "v3.create.project.hint", none: "v3.create.project.none" };
}

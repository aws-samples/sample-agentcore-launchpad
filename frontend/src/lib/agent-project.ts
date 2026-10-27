import type { EligibleAgentProject } from "./api";

/**
 * The project the create form starts on: the shell's current project when the
 * server listed it as eligible (active, not a `default` project, with a department,
 * and the caller may create agents in it), otherwise none — the member picks one.
 */
export function defaultAgentProjectId(
  eligible: readonly Pick<EligibleAgentProject, "id">[],
  currentId: string | null | undefined,
): string {
  return currentId && eligible.some((p) => p.id === currentId) ? currentId : "";
}

/** The picked project, if it is still one the server lists (a stale pick reads as none). */
export function pickedAgentProjectId(
  eligible: readonly Pick<EligibleAgentProject, "id">[],
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

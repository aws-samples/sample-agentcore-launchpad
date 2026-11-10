import type { TFunction } from "i18next";

import type { AssistantProposal } from "../../../lib/api";

/** N17-obs469: why approval is disabled when a live agent of the workspace already holds the
 *  revision's name (the backend's `name_conflict`, the rule `claim_agent_name` applies; its
 *  409 `agent.name_exists` stays the backstop) — undefined when the name is free. */
export function nameConflictReason(t: TFunction, proposal: AssistantProposal | null): string | undefined {
  const name = proposal?.name_conflict?.name;
  return name ? t("assistantPage.nameTaken", { name }) : undefined;
}

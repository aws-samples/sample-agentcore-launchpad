import { useTranslation } from "react-i18next";

import type { AssistantProposal } from "../../../lib/api";
import { Alert } from "../../ui";
import { nameConflictReason } from "./nameConflictReason";

/** N17-obs469: the persistent inline warning on a proposal whose name is taken (never only
 *  a toast). */
export function NameConflictAlert({ proposal }: { proposal: AssistantProposal | null }) {
  const { t } = useTranslation();
  const reason = nameConflictReason(t, proposal);
  if (!reason) return null;
  return <Alert tone="warn" testId="v2-assistant-name-conflict">{reason}</Alert>;
}

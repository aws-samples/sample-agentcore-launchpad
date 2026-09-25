import { useTranslation } from "react-i18next";

import { PageHeader } from "../ui";

/** Governance — native V2 page (scaffold; being rebuilt from the classic `/governance` page). */
export function V2Governance() {
  const { t } = useTranslation();
  return <PageHeader title={t("nav.governance")} />;
}

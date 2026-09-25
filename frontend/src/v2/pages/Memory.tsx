import { useTranslation } from "react-i18next";

import { PageHeader } from "../ui";

/** Memory — native V2 page (scaffold; being rebuilt from the classic `/memory` page). */
export function V2Memory() {
  const { t } = useTranslation();
  return <PageHeader title={t("nav.memory")} />;
}

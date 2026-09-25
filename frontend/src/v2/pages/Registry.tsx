import { useTranslation } from "react-i18next";

import { PageHeader } from "../ui";

/** Registry — native V2 page (scaffold; being rebuilt from the classic `/registry` page). */
export function V2Registry() {
  const { t } = useTranslation();
  return <PageHeader title={t("nav.registry")} />;
}

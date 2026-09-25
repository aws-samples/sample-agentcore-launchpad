import { useTranslation } from "react-i18next";

import { PageHeader } from "../ui";

/** Observability — native V2 page (scaffold; being rebuilt from the classic `/observability` page). */
export function V2Observability() {
  const { t } = useTranslation();
  return <PageHeader title={t("nav.observability")} />;
}

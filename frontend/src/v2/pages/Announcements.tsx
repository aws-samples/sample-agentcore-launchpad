import { useTranslation } from "react-i18next";

import { PageHeader } from "../ui";

/** Announcements — native V2 page (scaffold; being rebuilt from the classic `/announcements` page). */
export function V2Announcements() {
  const { t } = useTranslation();
  return <PageHeader title={t("nav.announcements")} />;
}

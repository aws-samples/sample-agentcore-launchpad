import { useTranslation } from "react-i18next";

import { PageHeader } from "../ui";

/** Videos — native V2 page (scaffold; being rebuilt from the classic `/videos` page). */
export function V2Videos() {
  const { t } = useTranslation();
  return <PageHeader title={t("nav.videos")} />;
}

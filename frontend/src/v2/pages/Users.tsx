import { useTranslation } from "react-i18next";

import { PageHeader } from "../ui";

/** Users — native V2 page (scaffold; being rebuilt from the classic `/users` page). */
export function V2Users() {
  const { t } = useTranslation();
  return <PageHeader title={t("nav.users")} />;
}

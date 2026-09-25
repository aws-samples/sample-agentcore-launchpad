import { useTranslation } from "react-i18next";

import { PageHeader } from "../ui";

/** Assistant — native V2 page (scaffold; being rebuilt from the classic `/create/assistant` page). */
export function V2Assistant() {
  const { t } = useTranslation();
  return <PageHeader title={t("nav.assistant")} />;
}

import { useTranslation } from "react-i18next";

import { PageHeader } from "../ui";

/** Chat — native V2 page (scaffold; being rebuilt from the classic `/chat` page). */
export function V2Chat() {
  const { t } = useTranslation();
  return <PageHeader title={t("nav.chat")} />;
}

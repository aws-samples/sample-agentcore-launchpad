import { useTranslation } from "react-i18next";

import { PageHeader } from "../ui";

/** KnowledgeBases — native V2 page (scaffold; being rebuilt from the classic `/knowledge-bases` page). */
export function V2KnowledgeBases() {
  const { t } = useTranslation();
  return <PageHeader title={t("nav.knowledgeBases")} />;
}

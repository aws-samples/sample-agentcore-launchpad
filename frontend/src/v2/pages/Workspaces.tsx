import { useTranslation } from "react-i18next";

import { PageHeader } from "../ui";

/** Workspaces — native V2 page (scaffold; being rebuilt from the classic `/workspaces` page). */
export function V2Workspaces() {
  const { t } = useTranslation();
  return <PageHeader title={t("nav.workspaces")} />;
}

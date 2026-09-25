import { useTranslation } from "react-i18next";

import { PageHeader } from "../ui";

/** SkillLab — native V2 page (scaffold; being rebuilt from the classic `/skill-lab` page). */
export function V2SkillLab() {
  const { t } = useTranslation();
  return <PageHeader title={t("nav.skillLab")} />;
}

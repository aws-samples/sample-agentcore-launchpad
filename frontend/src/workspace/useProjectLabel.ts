import { useCallback } from "react";
import { useTranslation } from "react-i18next";

import type { AccessProject } from "../lib/api/projects";
import { projectRole } from "./project-context";

/** "name · role" of a project option; an administrator outside it reads "admin access".
 *  Shared by the V2 and V3 project switchers. */
export function useProjectLabel() {
  const { t } = useTranslation();
  return useCallback(
    (project: AccessProject) => {
      const role = projectRole(project);
      const roleName = role ? t(`v3.project.roles.${role}`, { defaultValue: role }) : t("v3.project.adminAccess");
      return `${project.name} · ${roleName}`;
    },
    [t],
  );
}

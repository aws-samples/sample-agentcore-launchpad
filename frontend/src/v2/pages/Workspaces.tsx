import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { Alert, Card, PageHeader } from "../ui";
import { WorkspaceCreate } from "./workspaces/WorkspaceCreate";
import { WorkspaceDetail } from "./workspaces/WorkspaceDetail";
import { WorkspaceList } from "./workspaces/WorkspaceList";
import "./workspaces/workspaces.css";

/**
 * 工作区 — the environments (AWS account × region) the console can target:
 * register one (same account, or cross-account through an assumed spoke role),
 * drive its bootstrap job, grant it to members, detach or purge it. Admin-only.
 * Sub-pages are `?view=` states: `new`, `detail&id=[&gq=&granted=&gpage=]`.
 */
export function V2Workspaces() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const [params] = useSearchParams();
  const view = params.get("view");
  const id = params.get("id") ?? "";

  if (!isAdmin) {
    return (
      <>
        <PageHeader title={t("nav.workspaces")} />
        <Card testId="v2-workspaces-forbidden">
          <Alert tone="warn">{t("workspacesPage.forbiddenBody")}</Alert>
        </Card>
      </>
    );
  }
  if (view === "new") return <WorkspaceCreate />;
  // Keyed on the selection: the detail remembers which bootstrap job it watches,
  // so switching workspace must reset it rather than poll the previous job.
  if (view === "detail" && id) return <WorkspaceDetail key={id} workspaceId={id} />;
  return <WorkspaceList />;
}

import { ShieldAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { Card, PageHeader } from "../ui";
import { UserDetail } from "./users/UserDetail";
import { UserList } from "./users/UserList";
import "./users/users.css";

/**
 * 用户管理 — admin-only console account management: registration statistics,
 * approvals (with workspace grants), validity extension, enable/disable, role,
 * agent permissions, workspace grants, password reset and delete. The list keeps
 * `?status=`/`?q=`/`?page=`; one account is `?view=detail&user=<username>`.
 */
export function V2Users() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const [params] = useSearchParams();

  // Members never fire the admin-only requests (the backend would 403 them).
  if (!isAdmin) {
    return (
      <>
        <PageHeader title={t("nav.users")} desc={t("auth.adminRequired.meta")} />
        <Card>
          <div className="v2-users-forbidden" data-testid="users-forbidden-body">
            <ShieldAlert size={32} aria-hidden="true" />
            <b>{t("auth.adminRequired.title")}</b>
            <span>{t("usersPage.forbiddenBody")}</span>
          </div>
        </Card>
      </>
    );
  }

  const username = params.get("user");
  if (params.get("view") === "detail" && username) return <UserDetail key={username} username={username} />;
  return <UserList />;
}

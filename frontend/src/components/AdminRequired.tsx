import { ShieldAlert } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Panel } from "./Panel";
import { ViewHead } from "./ViewHead";

interface AdminRequiredProps {
  /** Page kicker, so the surface still reads as the page the user navigated to. */
  kicker: string;
  title: string;
  /** Why this specific module is restricted; falls back to the generic reason. */
  body?: string;
  testId?: string;
}

/**
 * Stand-in for a page a member may not use.
 *
 * The console's authorization model is read-only-for-members while there is no
 * per-user data partitioning, so several modules are administrator-only (see
 * backend `app/core/route_policy.py`). Rendering this instead of firing the
 * request keeps a member from meeting a bare 403.
 */
export function AdminRequired({ kicker, title, body, testId }: AdminRequiredProps) {
  const { t } = useTranslation();
  return (
    <>
      <ViewHead kicker={kicker} title={title} meta={t("auth.adminRequired.meta")} />
      <Panel brk title={t("auth.adminRequired.title")}>
        <div className="empty" data-testid={testId ?? "admin-required-body"}>
          <ShieldAlert size={18} aria-hidden="true" />{" "}
          {body ?? t("auth.adminRequired.body")}
        </div>
      </Panel>
    </>
  );
}

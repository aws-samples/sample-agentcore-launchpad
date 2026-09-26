import { ShieldAlert } from "lucide-react";
import { useCallback, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { Card, PageHeader } from "../ui";
import { AnnouncementEditor } from "./announcements/AnnouncementEditor";
import { AnnouncementList } from "./announcements/AnnouncementList";
import { NEW_ID } from "./announcements/common";
import { useAnnouncementSessions } from "./announcements/useSessions";
import "./announcements/announcements.css";

/**
 * 公告 — installation-wide notices shown to every user on the overview page (admin-only).
 * Sub-pages are `?view=` states: `new` (new draft) and `edit&id=` (edit / publish /
 * withdraw / delete). Editing buffers belong to this router, so they survive list ↔
 * editor navigation.
 */
export function V2Announcements() {
  const { isAdmin } = useAuth();
  const { t } = useTranslation();
  // Mount no loaders or editor state for a member, including direct deep links.
  if (!isAdmin) {
    return (
      <>
        <PageHeader title={t("announcements.title")} desc={t("auth.adminRequired.meta")} />
        <Card title={t("auth.adminRequired.title")}>
          <div className="v2-table-empty" data-testid="announcements-forbidden">
            <ShieldAlert size={28} aria-hidden="true" />
            <div>{t("auth.adminRequired.body")}</div>
          </div>
        </Card>
      </>
    );
  }
  return <AnnouncementManager />;
}

function AnnouncementManager() {
  const [params, setParams] = useSearchParams();
  const paramsRef = useRef(params);
  paramsRef.current = params;
  const store = useAnnouncementSessions();
  const [offset, setOffset] = useState(0);

  const view = params.get("view");
  const selected = view === "new" ? NEW_ID : view === "edit" ? params.get("id") : null;

  const open = useCallback(
    (id: string) => setParams(id === NEW_ID ? { view: "new" } : { view: "edit", id }),
    [setParams],
  );
  const back = useCallback(() => setParams({}), [setParams]);
  const moved = useCallback(
    (from: string, to: string | null) => {
      const current = paramsRef.current;
      const shown = current.get("view") === "new" ? NEW_ID : current.get("view") === "edit" ? current.get("id") : null;
      if (shown !== from) return;
      if (to) setParams({ view: "edit", id: to }, { replace: true });
      else setParams({});
    },
    [setParams],
  );

  if (selected) {
    return <AnnouncementEditor key={selected} id={selected} store={store} onBack={back} onMoved={moved} />;
  }
  return <AnnouncementList store={store} offset={offset} onOffset={setOffset} onOpen={open} />;
}

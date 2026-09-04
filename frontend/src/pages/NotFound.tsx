import { useTranslation } from "react-i18next";
import { Link, useLocation } from "react-router-dom";

import { Btn, Panel, ViewHead } from "../components";

/**
 * Catch-all view for URLs the router does not know (typos, stale bookmarks to
 * retired sub-routes). Rendered through the Shell's `<Outlet />`, so the chrome
 * stays and the only new thing on screen is the message plus a way back.
 */
export function NotFound() {
  const { t } = useTranslation();
  const { pathname } = useLocation();
  return (
    <>
      <ViewHead kicker={t("notFound.kicker")} title={t("notFound.title")} />
      <Panel title={t("notFound.panelTitle")} sub={t("notFound.panelSub")}>
        <p style={{ margin: "0 0 12px" }} data-testid="notfound-body">
          {t("notFound.body")}
        </p>
        <p style={{ margin: "0 0 18px" }}>
          <span className="mono" style={{ color: "var(--ink-3)" }}>
            {t("notFound.pathLabel")}{" "}
          </span>
          <span className="mono" data-testid="notfound-path">
            {pathname}
          </span>
        </p>
        <Link to="/" style={{ textDecoration: "none" }} data-testid="notfound-home">
          <Btn primary>{t("notFound.backToOverview")}</Btn>
        </Link>
      </Panel>
    </>
  );
}

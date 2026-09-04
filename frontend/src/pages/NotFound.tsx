import { useTranslation } from "react-i18next";
import { Link, useLocation } from "react-router-dom";

import { Panel, ViewHead } from "../components";

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
        {/* A Link styled as the house primary button — never a <button> inside
            an <a>, which is invalid interactive-content nesting. */}
        <Link to="/" className="btn primary" data-testid="notfound-home">
          {t("notFound.backToOverview")}
        </Link>
      </Panel>
    </>
  );
}

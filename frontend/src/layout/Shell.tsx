import { useTranslation } from "react-i18next";
import { matchPath, Outlet, useLocation } from "react-router-dom";

import { useWorkspace } from "../workspace/workspace-context";
import { ALL_NAV_ENTRIES, NAV_ENTRIES, ROUTE_PATHS } from "./nav";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import { useHealth } from "./useHealth";

function crumbKeyFor(pathname: string): string {
  // An unrouted path (typo, stale bookmark) renders the catch-all NotFound
  // view; label it as such instead of the nearest prefix match's module.
  if (!ROUTE_PATHS.some((pattern) => matchPath(pattern, pathname))) return "nav.notFound";
  const entry =
    ALL_NAV_ENTRIES.find((e) => e.to !== "/" && pathname.startsWith(e.to)) ??
    NAV_ENTRIES[0];
  return entry.labelKey;
}

export function Shell() {
  const location = useLocation();
  const { t } = useTranslation();
  const { health, status: healthStatus } = useHealth();
  const { current } = useWorkspace();

  return (
    <>
      <Topbar
        crumbKey={crumbKeyFor(location.pathname)}
        health={health}
        healthStatus={healthStatus}
      />
      <div className="layout">
        <Sidebar health={health} />
        <main>
          {/* Keyed on the workspace: switching discards every page's state in
              one place, so the ~20 mount-only loaders refetch against the new
              environment instead of showing the old one's rows. */}
          <div className="view" key={current?.id ?? "none"}>
            <Outlet />
            <footer>
              {t("footer.phase")}
              <span className="sep">|</span>
              {t("footer.payments")}
              <span className="sep">|</span>
              {t("footer.palette")}
            </footer>
          </div>
        </main>
      </div>
    </>
  );
}

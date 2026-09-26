import "./v2.css";
import "./v2-classic.css";

import { ChevronDown, LogOut, Repeat } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { RouteChunk } from "../layout/RouteChunk";
import { setUiVersion } from "../lib/ui-version";
import { useWorkspace } from "../workspace/workspace-context";
import { V2Logo } from "./Logo";
import { V2_NAV, type V2NavItem } from "./nav";
import { V2ToastProvider } from "./ui";

const COLLAPSE_KEY = "launchpad_v2_nav_collapsed";

function readCollapsed(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(COLLAPSE_KEY) ?? "{}") as Record<string, boolean>;
  } catch {
    return {};
  }
}

function isActive(item: V2NavItem, pathname: string, search: string): boolean {
  const [path, query] = item.to.split("?");
  if (query) {
    // an entry for a `?view=` sub-page is active only on that sub-page
    const want = new URLSearchParams(query);
    const have = new URLSearchParams(search);
    if (pathname !== path) return false;
    return [...want].every(([k, v]) => have.get(k) === v);
  }
  if (item.end) return pathname === path || pathname === `${path}/`;
  return [path, ...(item.also ?? [])].some(
    (prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`),
  );
}

function WorkspaceSelect() {
  const { t } = useTranslation();
  const { workspaces, current, select } = useWorkspace();
  if (workspaces.length === 0) return null;
  return (
    <label className="v2-filter" title={t("topbar.workspaceTitle")}>
      {t("topbar.workspaceLabel")}
      <b>{current ? `${current.name} · ${current.region}` : "—"}</b>
      <ChevronDown size={14} aria-hidden="true" />
      <select
        value={current?.id ?? ""}
        onChange={(e) => select(e.target.value)}
        aria-label={t("topbar.workspaceLabel")}
        data-testid="v2-workspace-select"
      >
        {workspaces.map((ws) => (
          <option key={ws.id} value={ws.id}>
            {ws.name} · {ws.region}
          </option>
        ))}
      </select>
    </label>
  );
}

function Lang() {
  const { i18n } = useTranslation();
  const lang = i18n.resolvedLanguage ?? "en";
  return (
    <div className="v2-lang">
      <button type="button" className={lang.startsWith("zh") ? "on" : ""} onClick={() => void i18n.changeLanguage("zh-CN")}>
        中文
      </button>
      <button type="button" className={lang === "en" ? "on" : ""} onClick={() => void i18n.changeLanguage("en")}>
        EN
      </button>
    </div>
  );
}

/**
 * Chrome of the V2 console: top bar (brand, workspace, language, user, switch
 * back to the classic console) and a grouped, collapsible sidebar. It wraps
 * both the native V2 pages under /v2 and — with `classic` — the classic
 * modules' pages, which render on the V2 light theme (v2-classic.css) until
 * they are rebuilt natively.
 */
export function V2Shell({ classic = false }: { classic?: boolean }) {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const { isAdmin, authRequired, username, logout } = useAuth();
  const { current } = useWorkspace();
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(readCollapsed);

  // Opening a /v2 page (a bookmark, a shared link) is choosing V2: the classic
  // modules reached from its sidebar then stay inside this shell too.
  useEffect(() => {
    if (!classic) setUiVersion("v2");
  }, [classic]);

  // The classic console styles <body> for its dark theme; V2 overrides it
  // only while mounted.
  useEffect(() => {
    document.body.classList.add("v2-body");
    return () => document.body.classList.remove("v2-body");
  }, []);

  const toggle = (key: string) => {
    setCollapsed((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      try {
        localStorage.setItem(COLLAPSE_KEY, JSON.stringify(next));
      } catch {
        // per-browser convenience only
      }
      return next;
    });
  };

  // A classic module stays on its page (the classic shell takes over the same
  // route); a native V2 page has no classic twin, so it lands on the overview.
  const switchToClassic = () => {
    setUiVersion("v1");
    if (!classic) navigate("/");
  };

  const displayName = authRequired ? (username ?? "—") : "operator";

  return (
    <div className="v2" data-testid="v2-shell">
      <V2ToastProvider>
        <header className="v2-top">
          <Link to="/v2" className="v2-brand">
            <V2Logo className="v2-brand-logo" />
            {t("v2.brand")}
            <small>V2</small>
          </Link>
          <nav className="v2-top-tabs" aria-label={t("v2.nav.products")}>
            <span className="on">{t("v2.nav.productAgents")}</span>
          </nav>
          <div className="v2-top-right">
            <WorkspaceSelect />
            <Lang />
            <button type="button" className="v2-btn sm" onClick={switchToClassic} data-testid="v2-switch-classic">
              <Repeat size={13} aria-hidden="true" />
              {t("v2.switchClassic")}
            </button>
            <div className="v2-user">
              <span className="v2-avatar">{displayName.slice(0, 1).toUpperCase()}</span>
              <span>{displayName}</span>
              {authRequired && (
                <button
                  type="button"
                  className="v2-link"
                  onClick={() => void logout()}
                  title={t("auth.logout")}
                  aria-label={t("auth.logout")}
                >
                  <LogOut size={14} />
                </button>
              )}
            </div>
          </div>
        </header>
        <div className="v2-layout">
          <aside className="v2-side" aria-label={t("v2.nav.label")}>
            {V2_NAV.map((group) => {
              const items = group.items.filter((item) => !item.admin || isAdmin);
              if (items.length === 0) return null;
              const closed = collapsed[group.key] === true;
              return (
                <div key={group.key} className="v2-side-group">
                  <button
                    type="button"
                    className="v2-side-head"
                    aria-expanded={!closed}
                    onClick={() => toggle(group.key)}
                  >
                    {t(group.labelKey)}
                    <ChevronDown size={14} className={closed ? "chev closed" : "chev"} aria-hidden="true" />
                  </button>
                  {!closed &&
                    items.map((item) => {
                      const Icon = item.icon;
                      const active = isActive(item, location.pathname, location.search);
                      return (
                        <Link
                          key={item.to}
                          to={item.to}
                          className={active ? "v2-side-item active" : "v2-side-item"}
                          aria-current={active ? "page" : undefined}
                          data-testid={`v2-nav-${item.to}`}
                        >
                          <Icon size={16} aria-hidden="true" />
                          {t(item.labelKey)}
                        </Link>
                      );
                    })}
                </div>
              );
            })}
          </aside>
          <div className="v2-main">
            {/* Workspace-bound pages refetch on selection; hub-global content
                and admin drafts survive a workspace switch. */}
            <div
              className={classic ? "v2-main-inner view v2-classic" : "v2-main-inner"}
              key={
                location.pathname === "/announcements" || location.pathname === "/v2/announcements" ||
                location.pathname === "/videos" || location.pathname === "/v2/videos" ||
                location.pathname === "/v2/video-management"
                  ? "hub-global-content"
                  : current?.id ?? "none"
              }
            >
              <RouteChunk key={location.pathname}>
                <Outlet />
              </RouteChunk>
            </div>
          </div>
        </div>
      </V2ToastProvider>
    </div>
  );
}

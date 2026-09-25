import "./v2.css";

import { ChevronDown, LogOut, Repeat } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { RouteChunk } from "../layout/RouteChunk";
import { setUiVersion } from "../lib/ui-version";
import { useWorkspace } from "../workspace/workspace-context";
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

function isActive(item: V2NavItem, pathname: string): boolean {
  if (!item.v2) return false;
  if (item.end) return pathname === item.to || pathname === `${item.to}/`;
  return pathname === item.to || pathname.startsWith(`${item.to}/`);
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
 * back to the classic console) and a grouped, collapsible sidebar. V2 pages
 * render in the content area; entries for modules not yet migrated navigate
 * to their classic page.
 */
export function V2Shell() {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const { isAdmin, authRequired, username, logout } = useAuth();
  const { current } = useWorkspace();
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(readCollapsed);

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

  const switchToClassic = () => {
    setUiVersion("v1");
    navigate("/");
  };

  const displayName = authRequired ? (username ?? "—") : "operator";

  return (
    <div className="v2" data-testid="v2-shell">
      <V2ToastProvider>
        <header className="v2-top">
          <Link to="/v2" className="v2-brand">
            <span className="v2-brand-logo">A</span>
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
                      const active = isActive(item, location.pathname);
                      return (
                        <NavLink
                          key={item.to}
                          to={item.to}
                          end={item.end}
                          className={active ? "v2-side-item active" : "v2-side-item"}
                          data-testid={`v2-nav-${item.to}`}
                          title={item.v2 ? undefined : t("v2.nav.classicHint")}
                        >
                          <Icon size={16} aria-hidden="true" />
                          {t(item.labelKey)}
                          {!item.v2 && <span className="legacy">{t("v2.nav.classic")}</span>}
                        </NavLink>
                      );
                    })}
                </div>
              );
            })}
          </aside>
          <div className="v2-main">
            <div className="v2-main-inner" key={current?.id ?? "none"}>
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

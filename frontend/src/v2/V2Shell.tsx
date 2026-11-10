import "./v2.css";
import "./v2-classic.css";

import { ChevronDown, LogOut, Menu, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { FeatureGate } from "../layout/FeatureGate";
import { RouteChunk } from "../layout/RouteChunk";
import { hasFeature, useFeatures } from "../lib/capabilities";
import { useWorkspace } from "../workspace/workspace-context";
import { bundleSha, cleanSha, isNewVersion } from "./buildSha";
import { V2Lang } from "./Lang";
import { V2Logo } from "./Logo";
import { isNavItemActive, V2_NAV } from "./nav";
import { fmtTime } from "./format";
import { NewVersionBanner } from "./NewVersionBanner";
import { RestartingNotice } from "./RestartingNotice";
import { ModuleDemoProvider } from "./pages/videos/ModuleDemo";
import { useRegistryPendingCount } from "./registryPending";
import { FilterSelect, V2ToastProvider } from "./ui";
import { useHealth } from "./useHealth";

const COLLAPSE_KEY = "launchpad_v2_nav_collapsed";

function readCollapsed(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(COLLAPSE_KEY) ?? "{}") as Record<string, boolean>;
  } catch {
    return {};
  }
}

/** The sidebar breakpoint — keep in step with `@media (max-width: 960px)` in v2.css. */
const NARROW_QUERY = "(max-width: 960px)";

/**
 * WB-004: below 960 px the sidebar is an overlay drawer behind a menu button
 * instead of simply disappearing. False wherever `matchMedia` is missing, so the
 * desktop markup (and the e2e test ids that resolve against it) is the default.
 */
function useNarrow(): boolean {
  const supported = typeof window !== "undefined" && typeof window.matchMedia === "function";
  const [narrow, setNarrow] = useState(() => (supported ? window.matchMedia(NARROW_QUERY).matches : false));
  useEffect(() => {
    if (!supported) return;
    const query = window.matchMedia(NARROW_QUERY);
    const onChange = (event: MediaQueryListEvent) => setNarrow(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, [supported]);
  return narrow;
}

function WorkspaceSelect() {
  const { t } = useTranslation();
  const { workspaces, current, select } = useWorkspace();
  if (workspaces.length === 0) return null;
  return (
    <FilterSelect
      label={t("topbar.workspaceLabel")}
      title={t("topbar.workspaceTitle")}
      value={current?.id ?? ""}
      options={workspaces.map((ws) => ({ value: ws.id, label: `${ws.name} · ${ws.region}` }))}
      onChange={select}
      testId="v2-workspace-select"
    />
  );
}

/**
 * Chrome of the console: top bar (brand, workspace, language, user) and a
 * grouped, collapsible sidebar. It wraps the native V2 pages under /v2 and —
 * with `classic` — the token-styled agent flows that have no native page yet
 * (system presets, Runtime import, a preset's configure flow, the Studio
 * canvas), which render on the V2 light theme through v2-classic.css.
 */
export function V2Shell({ classic = false }: { classic?: boolean }) {
  const { t } = useTranslation();
  const location = useLocation();
  const { isAdmin, authRequired, username, logout } = useAuth();
  const { current } = useWorkspace();
  const { features } = useFeatures();
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(readCollapsed);
  const narrow = useNarrow();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const drawerWasOpen = useRef(false);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const sideRef = useRef<HTMLElement>(null);

  // Closing hands focus back to the button that opened the drawer (Escape, the
  // backdrop, a nav link — the link itself is hidden with the drawer).
  const closeDrawer = useCallback(() => {
    setDrawerOpen(false);
    toggleRef.current?.focus();
  }, []);
  useEffect(() => {
    drawerWasOpen.current = drawerOpen;
  }, [drawerOpen]);
  // A route change closes the drawer (the page the user picked is behind it).
  useEffect(() => {
    if (drawerWasOpen.current) closeDrawer();
  }, [location.pathname, location.search, closeDrawer]);
  // Growing past the breakpoint turns the drawer back into the fixed sidebar.
  useEffect(() => {
    if (!narrow) setDrawerOpen(false);
  }, [narrow]);
  // While open: focus moves into the drawer, Escape closes it.
  useEffect(() => {
    if (!drawerOpen) return;
    sideRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeDrawer();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [drawerOpen, closeDrawer]);

  // The token-styled pages read the palette from <body> (v2-classic.css), so
  // portaled UI (toasts, dialogs, row menus) follows the light theme too.
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

  const displayName = authRequired ? (username ?? "—") : "operator";
  // N13-obs10: the deployed build sha, read off /api/health (`./useHealth`, the probe the
  // retired classic topbar's chip used) so an operator — or a demo's `recorded_version`
  // check — can verify which commit this console runs without ssh. Hidden while unknown.
  const { health, status: healthStatus } = useHealth();
  const build = health?.build;
  // N17-obs466 follow-up (deploy-ops-43): the label names THIS TAB's bundle — read off
  // /api/health it claimed the new build while an old tab still ran the old JS. The tooltip
  // adds the served console build (`dist_sha`) and the deployed commit; `stale` is the
  // banner's own verdict. A dev-server tab has no compiled sha and shows the deployed one.
  const tabSha = bundleSha();
  const servedSha = cleanSha(build?.dist_sha);
  const buildStale = isNewVersion(tabSha, servedSha);
  const buildLabel = tabSha ? tabSha.slice(0, 8) : build?.short ?? null;
  // N17-obs267: the pending-approval count on the 「注册中心」 entry — administrators only,
  // re-read on every route change and after a registry action, hidden at 0
  const registryPending = useRegistryPendingCount(
    isAdmin && hasFeature(features, "registry"),
    `${current?.id ?? ""}|${location.pathname}|${location.search}`,
  );

  return (
    <div className="v2" data-testid="v2-shell">
      <V2ToastProvider>
        {/* per-module demo videos (#222): one catalog load feeds every PageHeader button */}
        <ModuleDemoProvider>
          <header className="v2-top">
            {/* WB-004: shown by v2.css at ≤960 px only; opens the sidebar as a drawer */}
            <button
              type="button"
              ref={toggleRef}
              className="v2-nav-toggle"
              data-testid="v2-nav-toggle"
              aria-expanded={drawerOpen}
              aria-controls="v2-side"
              aria-label={t(drawerOpen ? "v2.nav.close" : "v2.nav.open")}
              title={t(drawerOpen ? "v2.nav.close" : "v2.nav.open")}
              onClick={() => (drawerOpen ? closeDrawer() : setDrawerOpen(true))}
            >
              {drawerOpen ? <X size={18} aria-hidden="true" /> : <Menu size={18} aria-hidden="true" />}
            </button>
            <Link to="/v2" className="v2-brand">
              <V2Logo className="v2-brand-logo" />
              {t("v2.brand")}
            </Link>
            <div className="v2-top-right">
              {/* ≤960 px: the secondary actions live in the drawer, so nothing is clipped */}
              {!narrow && (
                <>
                  <WorkspaceSelect />
                  <V2Lang />
                </>
              )}
              <div className="v2-user">
                <span className="v2-avatar">{displayName.slice(0, 1).toUpperCase()}</span>
                <span>{displayName}</span>
                {authRequired && !narrow && (
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
            {narrow && drawerOpen && (
              <div
                className="v2-drawer-backdrop"
                data-testid="v2-nav-backdrop"
                aria-hidden="true"
                onClick={closeDrawer}
              />
            )}
            <aside
              id="v2-side"
              ref={sideRef}
              tabIndex={-1}
              className={narrow && drawerOpen ? "v2-side open" : "v2-side"}
              aria-label={t("v2.nav.label")}
            >
              {V2_NAV.map((group) => {
                const items = group.items.filter(
                  (item) => (!item.admin || isAdmin) && (!item.feature || hasFeature(features, item.feature)),
                );
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
                        const active = isNavItemActive(item, location.pathname, location.search);
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
                            {item.badge === "registry_pending" && registryPending !== null && registryPending > 0 && (
                              <span
                                className="v2-side-badge"
                                data-testid="v2-nav-badge-registry"
                                title={t("v2.nav.registryPending", { count: registryPending })}
                                aria-label={t("v2.nav.registryPending", { count: registryPending })}
                              >
                                {registryPending > 99 ? "99+" : registryPending}
                              </span>
                            )}
                          </Link>
                        );
                      })}
                  </div>
                );
              })}
              {narrow && (
                <div className="v2-side-extras" data-testid="v2-side-extras">
                  <WorkspaceSelect />
                  <V2Lang />
                  {authRequired && (
                    <button
                      type="button"
                      className="v2-btn sm"
                      onClick={() => void logout()}
                      data-testid="v2-sign-out"
                    >
                      <LogOut size={13} aria-hidden="true" />
                      {t("auth.logout")}
                    </button>
                  )}
                </div>
              )}
              {buildLabel && (
                <div
                  className={buildStale ? "v2-side-build stale" : "v2-side-build v2-muted"}
                  data-testid="v2-build-sha"
                  data-sha={tabSha ?? build?.sha ?? undefined}
                  data-server-sha={servedSha ?? undefined}
                  data-stale={buildStale ? "true" : "false"}
                  title={t("v2.build.title", {
                    tab: tabSha ? tabSha.slice(0, 8) : "—",
                    served: servedSha ? servedSha.slice(0, 8) : "—",
                    sha: build?.sha ?? "—",
                    builtAt: build?.built_at ? fmtTime(build.built_at) : "—",
                  })}
                >
                  {t("v2.build.label", { sha: buildLabel })}
                  {buildStale && <span>{" · "}{t("v2.build.stale")}</span>}
                </div>
              )}
            </aside>
            <div className="v2-main">
              {/* N17-obs466: this tab runs an older bundle than the one now served */}
              <NewVersionBanner bundleSha={tabSha} servedSha={build?.dist_sha} />
              {/* N17-obs470: the backend is restarting (health fails) — non-blocking */}
              <RestartingNotice down={healthStatus === "down"} />
              {/* Workspace-bound pages refetch on selection; hub-global content
                  and admin drafts survive a workspace switch. */}
              <div
                className={classic ? "v2-main-inner view v2-classic" : "v2-main-inner"}
                key={
                  location.pathname === "/announcements" || location.pathname === "/v2/announcements" ||
                  location.pathname === "/videos" || location.pathname === "/v2/videos"
                    ? "hub-global-content"
                    : current?.id ?? "none"
                }
              >
                <RouteChunk key={location.pathname}>
                  <FeatureGate>
                    <Outlet />
                  </FeatureGate>
                </RouteChunk>
              </div>
            </div>
          </div>
        </ModuleDemoProvider>
      </V2ToastProvider>
    </div>
  );
}

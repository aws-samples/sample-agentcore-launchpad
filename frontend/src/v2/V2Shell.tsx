import "./v2.css";
import "./v2-classic.css";
import "./glossary.css";

import { ChevronDown, LogOut, Search } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, Outlet, useLocation } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { RouteChunk } from "../layout/RouteChunk";
import { navSettingsApi } from "../lib/api/navSettings";
import { setUiVersion } from "../lib/ui-version";
import { useNavMode } from "../lib/nav-mode";
import { effectiveHidden } from "../v3/nav";
import { NoBoundProjectBanner } from "../workspace/NoProjectAssigned";
import { groupByDepartment, scopeKey, showsProjectChip, splitByAccess, useProject } from "../workspace/project-context";
import { useProjectLabel } from "../workspace/useProjectLabel";
import { useWorkspace } from "../workspace/workspace-context";
import { CommandPalette, PALETTE_SHORTCUT } from "./CommandPalette";
import { GlossaryDialog } from "./GlossaryDialog";
import { HelpMenu } from "./HelpMenu";
import { useLoad } from "./hooks";
import { V2Lang } from "./Lang";
import { V2Logo } from "./Logo";
import { isNavItemActive, navGroupsFor } from "./nav";
import { TourHost } from "./onboarding/Tour";
import { closePalette, isPaletteToggle, isSlashOpen, openPalette, togglePalette } from "./palette";
import { ModuleDemoProvider } from "./pages/videos/ModuleDemo";
import { TierTag } from "./pages/workspaces/tags";
import { FilterSelect, Tag, V2ToastProvider } from "./ui";

const COLLAPSE_KEY = "launchpad_v2_nav_collapsed";

function readCollapsed(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(COLLAPSE_KEY) ?? "{}") as Record<string, boolean>;
  } catch {
    return {};
  }
}

/** A dialog, drawer, dropdown or menu is open: `/` then belongs to it, not the palette. */
function overlayOpen(): boolean {
  return document.querySelector('.v2 [role="dialog"][aria-modal="true"], .v2 .v2-pop, .v2 [role="menu"]') !== null;
}

function WorkspaceSelect() {
  const { t } = useTranslation();
  const { workspaces, current, select } = useWorkspace();
  if (workspaces.length === 0) return null;
  return (
    <>
      <FilterSelect
        label={t("topbar.workspaceLabel")}
        title={t("topbar.workspaceTitle")}
        value={current?.id ?? ""}
        options={workspaces.map((ws) => ({
          value: ws.id,
          // T05: a non-dev tier is part of the name, so prod is never picked by mistake
          label: `${ws.name} · ${ws.region}${
            ws.tier && ws.tier !== "dev" ? ` · ${t(`v2.workspaces.tier.${ws.tier}`)}` : ""
          }`,
        }))}
        onChange={select}
        testId="v2-workspace-select"
      />
      {current && (
        <span data-testid="v2-workspace-tier" data-tier={current.tier ?? "dev"}>
          <TierTag tier={current.tier} />
        </span>
      )}
    </>
  );
}

/**
 * Right of the workspace switch, as in V3 (projects §2.7): the project every scoped
 * page reads and writes in. Absent while there is no choice to make; a workspace that
 * binds several projects the caller cannot switch between shows the current one.
 */
function ProjectSelect() {
  const { t } = useTranslation();
  const { projects, current, switchable, boundProjects, select } = useProject();
  const label = useProjectLabel();
  if (!current) return null;
  if (!switchable) {
    if (!showsProjectChip(switchable, boundProjects, current)) return null;
    return (
      <span title={t("v3.project.label")} data-testid="v2-project-chip">
        <Tag tone="outline">{`${t("v3.project.label")} · ${label(current)}`}</Tag>
      </span>
    );
  }
  const { member, adminAccess } = splitByAccess(projects);
  // grouped by department once any project has one, members' projects first inside
  // each group — the order V3's switcher uses
  const departments = groupByDepartment([...member, ...adminAccess]);
  const options = departments.some((g) => g.id !== null)
    ? departments.flatMap((g) =>
        g.projects.map((p) => ({ value: p.id, label: label(p), group: g.label ?? t("v3.project.noDepartment") })))
    : [
        ...member.map((p) => ({
          value: p.id, label: label(p), group: adminAccess.length ? t("v3.project.memberGroup") : undefined,
        })),
        ...adminAccess.map((p) => ({ value: p.id, label: label(p), group: t("v3.project.adminGroup") })),
      ];
  return (
    <FilterSelect
      label={t("v3.project.label")}
      title={t("v3.project.label")}
      value={current.id}
      options={options}
      onChange={select}
      testId="v2-project-select"
    />
  );
}

/**
 * Chrome of the V2 console: top bar (brand, workspace, language, user, switch
 * to V3) and a grouped, collapsible sidebar. It wraps
 * both the native V2 pages under /v2 and — with `classic` — the classic
 * modules' pages, which render on the V2 light theme (v2-classic.css) until
 * they are rebuilt natively.
 */
export function V2Shell({ classic = false }: { classic?: boolean }) {
  const { t, i18n } = useTranslation();
  const location = useLocation();
  const { isAdmin, authRequired, username, logout } = useAuth();
  const { current } = useWorkspace();
  const { current: project } = useProject();
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(readCollapsed);
  // T11: members land on the pared-down sidebar, admins on the full one; either
  // can switch. A filter only — every route stays reachable by URL.
  const [navMode, setNavMode] = useNavMode(isAdmin);
  // the entries Menu settings hid (services/nav_settings) — the same hub-global list
  // and Normal default the V3 rail applies, for every account, administrators
  // included. A failed read hides nothing: the sidebar must never vanish because one
  // setting did not load.
  const hiddenLoad = useLoad(() => navSettingsApi.hiddenModules(), "v2-shell-hidden");
  const hidden = useMemo(() => new Set(effectiveHidden(hiddenLoad.data)), [hiddenLoad.data]);
  const navGroups = navGroupsFor(navMode, isAdmin, hidden);

  // The classic console styles <body> for its dark theme; V2 overrides it
  // only while mounted.
  useEffect(() => {
    document.body.classList.add("v2-body");
    return () => document.body.classList.remove("v2-body");
  }, []);

  // ⌘K / Ctrl+K toggles the command palette anywhere in V2 (even while typing);
  // `/` opens it outside text fields and open overlays.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (isPaletteToggle(e)) {
        e.preventDefault();
        togglePalette();
      } else if (isSlashOpen(e, document.activeElement as HTMLElement | null, overlayOpen())) {
        e.preventDefault();
        openPalette();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      closePalette();
    };
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

  return (
    <div className="v2" data-testid="v2-shell">
      <V2ToastProvider>
        <ModuleDemoProvider>
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
              <button
                type="button"
                className="v2-cmdk-trigger"
                onClick={openPalette}
                aria-label={t("v2.palette.search")}
                aria-keyshortcuts="Meta+K Control+K"
                title={`${t("v2.palette.search")} (${PALETTE_SHORTCUT})`}
                data-testid="v2-cmdk-open"
                data-tour="cmdk"
              >
                <Search size={14} aria-hidden="true" />
                <span>{t("v2.palette.search")}</span>
                <kbd aria-hidden="true">{PALETTE_SHORTCUT}</kbd>
              </button>
              <WorkspaceSelect />
              <ProjectSelect />
              <V2Lang />
              <button
                type="button"
                className="v2-btn sm"
                // every V2 page has a V3 home (hosted or rebuilt): stay on this one
                onClick={() => setUiVersion("v3")}
                data-testid="v2-switch-v3"
                title={t("v3.switch.tryHint")}
              >
                {t("v3.switch.try")}
              </button>
              <HelpMenu />
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
            <aside className="v2-side" aria-label={t("v2.nav.label")} data-tour="modules">
              {navGroups.map((group) => {
                const items = group.items;
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
                            title={item.hintKey && i18n.exists(item.hintKey) ? t(item.hintKey) : undefined}
                          >
                            <Icon size={16} aria-hidden="true" />
                            {t(item.labelKey)}
                          </Link>
                        );
                      })}
                  </div>
                );
              })}
              <button
                type="button"
                className="v2-side-mode"
                data-testid="v2-nav-mode"
                title={t("v2.navMode.hint")}
                onClick={() => setNavMode(navMode === "business" ? "expert" : "business")}
              >
                {navMode === "business" ? t("v2.nav.showExpert") : t("v2.nav.showBusiness")}
              </button>
            </aside>
            <div className="v2-main">
              {/* Workspace-bound pages refetch on a workspace or project switch; hub-global content
                  and admin drafts survive a workspace switch. */}
              <div
                className={classic ? "v2-main-inner view v2-classic" : "v2-main-inner"}
                key={
                  location.pathname === "/announcements" || location.pathname === "/v2/announcements" ||
                  location.pathname === "/videos" || location.pathname === "/v2/videos" ||
                  location.pathname === "/v2/video-management"
                    ? "hub-global-content"
                    : scopeKey(current?.id, project?.id)
                }
              >
                <NoBoundProjectBanner />
                <RouteChunk key={location.pathname}>
                  <Outlet />
                </RouteChunk>
              </div>
            </div>
          </div>
          {/* opened by a Term click, the palette or the help menu */}
          <GlossaryDialog />
          <CommandPalette hidden={hidden} />
          {/* first Home visit, or replayed from the help menu / palette */}
          <TourHost />
        </ModuleDemoProvider>
      </V2ToastProvider>
    </div>
  );
}

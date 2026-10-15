import "./v2.css";
import "./v2-classic.css";

import { ChevronDown, Repeat, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { RouteChunk } from "../layout/RouteChunk";
import { useNavMode } from "../lib/nav-mode";
import { setUiVersion } from "../lib/ui-version";
import { useWorkspace } from "../workspace/workspace-context";
import { BudgetBanner } from "./BudgetBanner";
import { CommandPalette, PALETTE_SHORTCUT } from "./CommandPalette";
import { GlossaryDialog } from "./Glossary";
import { HelpMenu } from "./HelpMenu";
import { V2Lang } from "./Lang";
import { V2Logo } from "./Logo";
import { isNavItemActive, navGroupsFor } from "./nav";
import { TourHost } from "./onboarding/Tour";
import { closePalette, isPaletteToggle, isSlashOpen, openPalette, togglePalette } from "./palette";
import { ModuleDemoProvider } from "./pages/videos/ModuleDemo";
import { FilterSelect, V2ToastProvider } from "./ui";
import { UserMenu } from "./UserMenu";

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
 * Chrome of the V2 console: top bar (brand, workspace, language, user, switch
 * back to the classic console) and a grouped, collapsible sidebar. It wraps
 * both the native V2 pages under /v2 and — with `classic` — the classic
 * modules' pages, which render on the V2 light theme (v2-classic.css) until
 * they are rebuilt natively.
 */
export function V2Shell({ classic = false }: { classic?: boolean }) {
  const { t, i18n } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const { isAdmin, authRequired, username, logout } = useAuth();
  const { current } = useWorkspace();
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(readCollapsed);
  // A view filter only (every route stays reachable by URL); everyone starts on
  // the full sidebar and may opt into the essentials.
  const [navMode, setNavMode] = useNavMode();
  const navGroups = navGroupsFor(navMode, isAdmin);

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
              <V2Lang />
              <HelpMenu />
              <button type="button" className="v2-btn sm" onClick={switchToClassic} data-testid="v2-switch-classic">
                <Repeat size={13} aria-hidden="true" />
                {t("v2.switchClassic")}
              </button>
              <UserMenu displayName={displayName} authRequired={authRequired} onLogout={() => void logout()} />
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
                {navMode === "business" ? t("v2.navMode.showExpert") : t("v2.navMode.showBusiness")}
              </button>
            </aside>
            <div className="v2-main">
              {/* own spend at 80 % of the monthly budget or over it (myUsage.ts) */}
              <BudgetBanner who={displayName} />
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
          {/* opened by a Term click, the palette or the help menu */}
          <GlossaryDialog />
          <CommandPalette />
          {/* first Home visit, or replayed from the help menu / palette */}
          <TourHost />
        </ModuleDemoProvider>
      </V2ToastProvider>
    </div>
  );
}

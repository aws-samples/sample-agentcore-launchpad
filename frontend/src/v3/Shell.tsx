import "@fontsource-variable/archivo/wdth.css";
import "../v2/v2.css";
import "../v2/glossary.css";
import "./v3.css";
import "./host.css";
import "./onboarding/onboarding.css";

import {
  ArrowLeftRight,
  BookOpen,
  Bot,
  ChevronRight,
  CircleHelp,
  FolderKanban,
  Compass,
  LayoutDashboard,
  KeyRound,
  ListChecks,
  LogOut,
  MessagesSquare,
  Menu,
  PlayCircle,
  Scale,
  Search,
  X,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { hasConsolePassword, useAuth } from "../auth/auth-context";
import { RouteChunk } from "../layout/RouteChunk";
import { api } from "../lib/api";
import { isSteward } from "../lib/api/steward";
import { systemUpdateApi } from "../lib/api/systemUpdate";
import { navSettingsApi } from "../lib/api/navSettings";
import type { AccessProject } from "../lib/api/projects";
import { useNavMode } from "../lib/nav-mode";
import { setUiVersion } from "../lib/ui-version";
import { GLOSSARY_TERMS } from "../v2/Glossary";
import { ModuleDemoProvider } from "../v2/pages/videos/ModuleDemo";
import { V2ToastProvider } from "../v2/ui";
import { groupByDepartment, projectRole, scopeKey, showsProjectChip, splitByAccess, useProject } from "../workspace/project-context";
import { NoBoundProjectBanner } from "../workspace/NoProjectAssigned";
import { StewardEmptyState } from "../workspace/StewardEmptyState";
import { showsStewardEmptyState } from "../workspace/steward-paths";
import { useWorkspace } from "../workspace/workspace-context";
import { v2CounterpartOf } from "./backToV2";
import { ChangePasswordDialog } from "./ChangePassword";
import { type Command, CommandPalette } from "./CommandPalette";
import { ToastProvider } from "./ui";
import { useLoad } from "./hooks";
import { Lang } from "./Lang";
import { ThemeSwitch } from "./ThemeSwitch";
import { HiddenNavContext } from "./hiddenNav";
import { effectiveHidden, HOME_SECTIONS, hostedGroups, isActive, withoutHidden, withoutHiddenPinned } from "./nav";
import { GlossaryDialog } from "./onboarding/Glossary";
import { openGlossary, requestTour, tourDone, useLaunchHidden, useTourRequest } from "./onboarding/state";
import { Tour } from "./onboarding/Tour";
import { DLC_TERMS } from "./story/terms";
import { Select } from "./select";

const RAIL_KEY = "launchpad_v3_rail_open";

function readOpen(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(RAIL_KEY) ?? "{}") as Record<string, boolean>;
  } catch {
    return {};
  }
}

interface RailItem {
  to: string;
  labelKey: string;
  icon: ReactNode;
  end?: boolean;
}

const NATIVE: RailItem[] = [
  { to: "/v3", labelKey: "v3.nav.home", icon: <LayoutDashboard size={16} />, end: true },
  { to: "/v3/agents", labelKey: "v3.nav.agents", icon: <Bot size={16} /> },
  { to: "/v3/chat", labelKey: "v3.nav.chat", icon: <MessagesSquare size={16} /> },
  { to: "/v3/gate", labelKey: "v3.nav.gate", icon: <Scale size={16} /> },
];

/** "?" in the top bar: the tour, the glossary, the launch sequence and the videos, any time.
 *  `launch={false}` drops the launch-sequence item, for when Menu settings hides that section. */
function HelpMenu({ launch = true }: { launch?: boolean }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { current } = useWorkspace();
  const [, setLaunchHidden] = useLaunchHidden(current?.id ?? "");
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [open]);
  const item = (icon: ReactNode, label: string, run: () => void) => (
    <button type="button" role="menuitem" onClick={() => { setOpen(false); run(); }}>{icon} {label}</button>
  );
  return (
    <div className="v3-help" data-tour="help" onClick={(e) => e.stopPropagation()}>
      <button type="button" className="v3-btn ghost sm" aria-haspopup="menu" aria-expanded={open}
        aria-label={t("v3.onboard.help.label")} title={t("v3.onboard.help.label")} onClick={() => setOpen((v) => !v)}>
        <CircleHelp size={15} />
      </button>
      {open && (
        <div className="v3-help-menu" role="menu">
          {item(<Compass size={15} />, t("v3.onboard.help.tour"), requestTour)}
          {launch && item(<ListChecks size={15} />, t("v3.onboard.help.launch"), () => { setLaunchHidden(false); navigate("/v3"); })}
          {item(<BookOpen size={15} />, t("v3.onboard.help.glossary"), () => openGlossary("all"))}
          {item(<PlayCircle size={15} />, t("v3.onboard.help.videos"), () => navigate("/v3/videos"))}
        </div>
      )}
    </div>
  );
}

function WorkspaceSwitch() {
  const { t } = useTranslation();
  const { workspaces, current, select } = useWorkspace();
  if (workspaces.length === 0) return null;
  return (
    <label className="v3-ws" data-tier={current?.tier ?? "dev"} title={t("v3.top.workspace")}>
      <span className="v3-ws-label">{t("v3.top.workspace")}</span>
      <Select className="" value={current?.id ?? ""} onChange={(e) => select(e.target.value)} aria-label={t("v3.top.workspace")}>
        {workspaces.map((ws) => (
          <option key={ws.id} value={ws.id}>
            {ws.name} · {ws.region}
            {ws.tier && ws.tier !== "dev" ? ` · ${ws.tier}` : ""}
          </option>
        ))}
      </Select>
    </label>
  );
}

/** "name · role" of a project option; an administrator outside it reads "admin access". */
function useProjectLabel() {
  const { t } = useTranslation();
  return useCallback(
    (project: AccessProject) => {
      const role = projectRole(project);
      const roleName = role ? t(`v3.project.roles.${role}`, { defaultValue: role }) : t("v3.project.adminAccess");
      return `${project.name} · ${roleName}`;
    },
    [t],
  );
}

/** Right of the workspace switch (projects §2.7); absent while there is no choice to make. */
function ProjectSwitch() {
  const { t } = useTranslation();
  const { projects, current, switchable, boundProjects, select } = useProject();
  const label = useProjectLabel();
  if (!switchable) {
    // hidden, yet the workspace binds several: the current project as a read-only chip
    if (!current || !showsProjectChip(switchable, boundProjects, current)) return null;
    return (
      <span className="v3-ws" title={t("v3.project.label")} data-testid="v3-project-chip">
        <span className="v3-ws-label">{t("v3.project.label")}</span>
        <span className="v3-chip">{label(current)}</span>
      </span>
    );
  }
  const { member, adminAccess } = splitByAccess(projects);
  const option = (p: AccessProject) => (
    <option key={p.id} value={p.id}>{label(p)}</option>
  );
  // §2.7: grouped by department once any project has one; members' projects first
  // inside each group, an administrator's non-member ones read "admin access"
  const departments = groupByDepartment([...member, ...adminAccess]);
  const byDepartment = departments.some((g) => g.id !== null);
  return (
    <label className="v3-ws" title={t("v3.project.label")} data-testid="v3-project-switch">
      <span className="v3-ws-label">{t("v3.project.label")}</span>
      <Select className="" value={current?.id ?? ""} onChange={(e) => select(e.target.value)} aria-label={t("v3.project.label")}>
        {byDepartment ? (
          departments.map((g) => (
            <optgroup key={g.id ?? "-"} label={g.label ?? t("v3.project.noDepartment")}>{g.projects.map(option)}</optgroup>
          ))
        ) : adminAccess.length === 0 ? (
          member.map(option)
        ) : (
          <>
            {member.length > 0 && <optgroup label={t("v3.project.memberGroup")}>{member.map(option)}</optgroup>}
            <optgroup label={t("v3.project.adminGroup")}>{adminAccess.map(option)}</optgroup>
          </>
        )}
      </Select>
    </label>
  );
}

/**
 * Console V3 — the command center. A fixed top bar (brand, ⌘K, workspace,
 * language, V2 switch, account) over a rail and the page. Pages V3 has not
 * rebuilt are still in the rail, marked, and open in V2 rather than being
 * wrapped in a shell whose look they do not share.
 */
/** Pages whose content belongs to the installation, not a workspace: they must not
 *  remount (and lose an admin's draft) when the workspace switches. */
const HUB_GLOBAL = new Set([
  "/announcements", "/v2/announcements", "/v3/announcements", "/videos", "/v2/videos", "/v3/videos", "/v2/video-management", "/v3/video-management",
  // people and structure (projects §7.9): never bound to the workspace or project in the switcher
  "/v3/users", "/v3/admins", "/v3/projects", "/v3/departments",
  // the console host itself (online-update): one installation, not a workspace
  "/v3/system-update",
]);

/** The running commit (online-update §2.6), for whoever reports a problem. */
function FooterVersion() {
  const { t } = useTranslation();
  const version = useLoad(() => systemUpdateApi.version(), "v3-footer-version");
  const short = version.data?.short;
  if (!short) return null;
  return (
    <span className="mono" data-testid="v3-footer-version" title={t("v3.systemUpdate.footer", { sha: version.data?.sha ?? short })}
      style={{ color: "var(--v3-text-3)", fontSize: 11, marginLeft: 8 }}>
      {short}
    </span>
  );
}

export function V3Shell({ hosted }: { hosted?: "v2" | "classic" } = {}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { isAdmin, role, authRequired, username, builtin, accountSource, logout } = useAuth();
  // a steward (organization admin, §11.4) manages structure only: its rail is
  // Configure, and every business page answers with its empty state
  const steward = isSteward(role);
  const stewardEmpty = showsStewardEmptyState(role, location.pathname);
  const [changingPassword, setChangingPassword] = useState(false);
  const { current } = useWorkspace();
  const project = useProject();
  const projectLabel = useProjectLabel();
  // workspace-bound pages remount (and refetch) on a workspace or project switch
  const scope = scopeKey(current?.id, project.current?.id);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [railOpen, setRailOpen] = useState(false);
  const railToggle = useRef<HTMLButtonElement>(null);
  useEffect(() => setRailOpen(false), [location.pathname, location.search]);
  useEffect(() => {
    if (!railOpen) return;
    const close = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        setRailOpen(false);
        railToggle.current?.focus();
      }
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [railOpen]);
  // a steward reads no agent (the count on the rail would be a refused request)
  const agents = useLoad(() => (steward ? Promise.resolve({ agents: [] }) : api.listAgents()), `v3-shell-agents:${scope}`);

  // Opening a /v3 page is choosing V3 — reloads and "/" land back here.
  useEffect(() => {
    setUiVersion("v3");
    document.body.classList.add("v3-body");
    return () => document.body.classList.remove("v3-body");
  }, []);

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      const typing = /input|textarea|select/i.test((e.target as HTMLElement)?.tagName ?? "");
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((open) => !open);
      } else if (e.key === "/" && !typing) {
        e.preventDefault();
        setPaletteOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // A hosted page has the same URL in V2, which simply re-renders it in the V2
  // shell; a native V3 page goes to its V2 counterpart (same record or tab).
  const backToV2 = () => {
    setUiVersion("v2");
    const to = v2CounterpartOf(location.pathname, location.search);
    if (to) navigate(to);
  };

  // business mode shows the build → run essentials; expert shows every module.
  // A view filter only: every page stays reachable by URL and by ⌘K.
  const [navMode, setNavMode] = useNavMode(isAdmin);
  // a `perm` rail entry (Project settings) shows for a holder in any project here
  const perms = useMemo(() => [...new Set(project.projects.flatMap((p) => p.permissions))], [project.projects]);
  // a department administrator (§11.1) also sees Projects, Project groups and Departments
  const managesDepartment = project.managedDepartments.length > 0;
  // the entries an administrator hid (services/nav_settings), for every account,
  // administrators included; a steward's rail has only administrator-only entries,
  // which are never hidden, so it skips the read. A failed read hides nothing — the
  // rail must never vanish because one setting did not load. A console that never saved
  // Menu settings applies the Normal preset (`effectiveHidden`); a steward's stand-in
  // answer is "saved, nothing hidden", so the default never reaches its rail
  const hiddenLoad = useLoad(
    () => (steward
      ? Promise.resolve({ hidden: [] as string[], updated_by: "", updated_at: "" })
      : navSettingsApi.hiddenModules()),
    `v3-shell-hidden:${steward}`,
  );
  const hidden = useMemo(() => new Set(effectiveHidden(hiddenLoad.data)), [hiddenLoad.data]);
  // answered (or failed, which hides nothing): until then the Command center holds back
  // the sections the list may hide, so they do not flash in and out
  const hiddenReady = steward || hiddenLoad.data !== null || hiddenLoad.error !== null;
  const hiddenNav = useMemo(
    () => ({ hidden, ready: hiddenReady, isAdmin, reload: hiddenLoad.reload }),
    [hidden, hiddenReady, isAdmin, hiddenLoad.reload],
  );
  // a hidden pinned entry (指挥中心, 放行门) leaves the rail and ⌘K; `/v3` still opens
  const native = useMemo(
    () => (steward ? NATIVE.filter((item) => item.to === "/v3") : withoutHiddenPinned(NATIVE, hidden)),
    [steward, hidden],
  );
  const allGroups = useMemo(
    () => withoutHidden(hostedGroups(isAdmin, perms, managesDepartment, steward), hidden),
    [isAdmin, perms, managesDepartment, steward, hidden],
  );
  const groups = useMemo(
    () =>
      // a steward's rail is the structure entries alone: no business slice to pare down to
      navMode === "expert" || steward
        ? allGroups
        : allGroups
            .map((g) => ({ ...g, items: g.items.filter((item) => item.business === true) }))
            .filter((g) => g.items.length > 0),
    [allGroups, navMode, steward],
  );
  // the tour opens on its own on a first visit to the command center, and on request
  const [touring, setTouring] = useState(false);
  const tourAsk = useTourRequest();
  useEffect(() => {
    if (tourAsk === 0) return;
    if (location.pathname !== "/v3") navigate("/v3");
    const timer = window.setTimeout(() => setTouring(true), 700);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only a new request opens it
  }, [tourAsk]);
  useEffect(() => {
    // the tour walks the business console, which a steward never sees
    if (location.pathname !== "/v3" || tourDone() || steward) return;
    // after the page has drawn, so every stop can be measured
    const timer = window.setTimeout(() => setTouring(true), 1200);
    return () => window.clearTimeout(timer);
  }, [location.pathname, steward]);
  // the module groups start folded, except the one holding the current page
  const [open, setOpen] = useState<Record<string, boolean>>(readOpen);
  const toggle = (key: string, wasOpen: boolean) =>
    setOpen((prev) => {
      const next = { ...prev, [key]: !wasOpen };
      try {
        localStorage.setItem(RAIL_KEY, JSON.stringify(next));
      } catch {
        // per-browser convenience only
      }
      return next;
    });

  const pages = useMemo<Command[]>(() => {
    const group = t("v3.cmdk.pages");
    const all = [
      ...native.map((item) => ({ to: item.to, labelKey: item.labelKey, icon: item.icon })),
      ...allGroups.flatMap((g) =>
        g.items.map(({ to, labelKey, icon: Icon }) => ({ to, labelKey, icon: <Icon size={16} /> })),
      ),
    ];
    return all.map((item) => ({
      id: `p:${item.to}`,
      group,
      label: t(item.labelKey),
      icon: item.icon,
      // the English name and the route, so "chat" finds 对话 under zh-CN
      keywords: `${t(item.labelKey, { lng: "en" })} ${item.to.replace(/[/?=&]/g, " ")}`,
      run: () => navigate(item.to),
    }));
  }, [allGroups, native, navigate, t]);

  // help in the palette: the tour, the glossary, and one entry per term, so typing
  // a word you do not know ("harness", "金标集") explains it
  const help = useMemo<Command[]>(() => {
    const group = t("v3.onboard.help.group");
    return [
      { id: "h:tour", group, label: t("v3.onboard.help.tour"), icon: <Compass size={16} />, keywords: "tour guide help 导览 引导", run: requestTour },
      { id: "h:glossary", group, label: t("v3.onboard.help.glossary"), icon: <BookOpen size={16} />, keywords: "glossary terms help 术语", run: () => openGlossary("all") },
      ...GLOSSARY_TERMS.map((term) => ({
        id: `g:${term}`,
        group,
        label: t("v3.onboard.glossary.explain", { term: t(`glossary.name.${term}`) }),
        icon: <BookOpen size={16} />,
        keywords: `${term} ${t(`glossary.name.${term}`, { lng: "en" })}`,
        run: () => openGlossary(term),
      })),
      // the Agent-DLC words ("golden set", "waiver"): the dialog opens on the term's card
      ...DLC_TERMS.map((term) => ({
        id: `d:${term}`,
        group,
        label: t("v3.onboard.glossary.explain", { term: t(`story.term.${term}.name`) }),
        icon: <BookOpen size={16} />,
        keywords: `${term} ${t(`story.term.${term}.name`, { lng: "en" })} ${t(`story.term.${term}.short`)}`,
        run: () => openGlossary(term),
      })),
    ];
  }, [t]);

  // ⌘K "switch project": one entry per other project, only when the switcher shows
  const projectCommands = useMemo<Command[]>(() => {
    if (!project.switchable) return [];
    const group = t("v3.project.cmdkGroup");
    return project.projects
      .filter((p) => p.id !== project.current?.id)
      .map((p) => ({
        id: `prj:${p.id}`,
        group,
        label: t("v3.project.switchTo", { name: p.name }),
        hint: projectLabel(p),
        icon: <FolderKanban size={16} />,
        keywords: `${p.id} ${t("v3.project.switchTo", { name: p.name, lng: "en" })} switch project 切换项目`,
        run: () => project.select(p.id),
      }));
  }, [project, projectLabel, t]);

  const managingAgent =
    location.pathname === "/v2/agents" && new URLSearchParams(location.search).get("view") !== "new";
  const closePalette = useCallback(() => setPaletteOpen(false), []);
  const displayName = authRequired ? (username ?? "—") : "operator";
  const failing = (agents.data?.agents ?? []).filter((a) => a.status === "failed").length;

  return (
    <div className="v3 v3-shell" data-testid="v3-shell" data-rail-open={railOpen}>
      <ToastProvider>
        <header className="v3-top">
          <button ref={railToggle} type="button" className="v3-btn ghost sm v3-rail-toggle"
            aria-label={t(railOpen ? "v3.ux.closeNav" : "v3.ux.openNav")}
            aria-expanded={railOpen} aria-controls="v3-navigation" onClick={() => setRailOpen((v) => !v)}>
            {railOpen ? <X size={18} /> : <Menu size={18} />}
          </button>
          <Link to="/v3" className="v3-brand">
            <span className="v3-brand-mark" aria-hidden="true" />
            Launchpad
            <sup>V3</sup>
          </Link>
          <button type="button" className="v3-cmdk-trigger" data-tour="cmdk" aria-label={t("v3.top.search")} title={t("v3.top.search")} onClick={() => setPaletteOpen(true)}>
            <Search size={15} aria-hidden="true" />
            <span>{t("v3.top.search")}</span>
            <kbd>⌘K</kbd>
          </button>
          <div className="v3-top-right">
            <WorkspaceSwitch />
            <ProjectSwitch />
            <Lang />
            <ThemeSwitch />
            <HelpMenu launch={!hidden.has(HOME_SECTIONS.launch)} />
            <button type="button" className="v3-btn sm v3-version-switch" onClick={backToV2} data-testid="v3-switch-v2" aria-label={t("v3.switch.back")} title={t("v3.switch.back")}>
              <ArrowLeftRight size={13} aria-hidden="true" />
              <span>{t("v3.switch.back")}</span>
            </button>
            <span className="v3-chip v3-account" title={displayName}>
              {displayName}
            </span>
            {/* an SSO account's password lives at its identity provider (sso §2.7) */}
            {hasConsolePassword(authRequired, accountSource) && (
              <button type="button" className="v3-btn ghost sm" onClick={() => setChangingPassword(true)} data-testid="v3-change-password"
                aria-label={t("v3.account.password.title")} title={t("v3.account.password.title")}>
                <KeyRound size={14} />
              </button>
            )}
            {authRequired && (
              <button type="button" className="v3-btn ghost sm" onClick={() => void logout()} aria-label={`${t("auth.logout")} · ${displayName}`} title={displayName}>
                <LogOut size={14} />
              </button>
            )}
          </div>
        </header>

        <nav id="v3-navigation" className="v3-rail" aria-label={t("v3.nav.label")}>
          {native.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              // an agent's V2 management page (hosted) still belongs to Agents
              className={({ isActive: on }) => (on || (item.to === "/v3/agents" && managingAgent) ? "on" : "")}
            >
              {item.icon}
              {t(item.labelKey)}
              {item.to === "/v3/agents" && failing > 0 && <span className="count">{failing}</span>}
            </NavLink>
          ))}
          <div data-tour="modules" style={{ display: "grid", gap: 2 }}>
          <div className="v3-rail-label">{t("v3.nav.more")}</div>
          {groups.map((group) => {
            const current = group.items.some((item) => isActive(item, location.pathname, location.search));
            const isOpen = open[group.key] ?? current;
            return (
              <div key={group.key} className="v3-rail-group">
                <button
                  type="button"
                  className="v3-rail-item v3-rail-group-head"
                  aria-expanded={isOpen}
                  onClick={() => toggle(group.key, isOpen)}
                  data-testid={`v3-rail-group-${group.key}`}
                >
                  <ChevronRight size={14} className="chev" aria-hidden="true" />
                  {t(group.labelKey)}
                  <span className="ext">{group.items.length}</span>
                </button>
                {isOpen &&
                  group.items.map((item) => {
                    const Icon = item.icon;
                    const on = isActive(item, location.pathname, location.search);
                    return (
                      <Link
                        key={item.to}
                        to={item.to}
                        className={on ? "v3-rail-sub on" : "v3-rail-sub"}
                        aria-current={on ? "page" : undefined}
                        title={item.hintKey ? t(item.hintKey) : undefined}
                      >
                        <Icon size={15} aria-hidden="true" />
                        {t(item.labelKey)}
                      </Link>
                    );
                  })}
              </div>
            );
          })}
          {!steward && (
            <button type="button" className="v3-rail-mode" data-testid="v3-nav-mode"
              title={t("v3.onboard.mode.hint")}
              onClick={() => setNavMode(navMode === "business" ? "expert" : "business")}>
              {navMode === "business" ? t("v3.onboard.mode.showExpert") : t("v3.onboard.mode.showBusiness")}
            </button>
          )}
          </div>
          <div className="v3-rail-foot">
            <span className="mono" style={{ color: "var(--v3-text-3)", fontSize: 11 }}>
              {current ? `${current.region}` : ""}
            </span>
            <FooterVersion />
          </div>
        </nav>

        <main className={hosted ? "v3-main hosted" : "v3-main"}>
          <HiddenNavContext.Provider value={hiddenNav}>
            {hosted === "v2" ? (
              <V2ToastProvider>
                {/* workspace-bound pages refetch on a switch; hub-global ones keep drafts */}
                {/* V2's per-module demo video rides in the hosted page header too */}
                <ModuleDemoProvider>
                  <div
                    className="v2 v3-host"
                    key={HUB_GLOBAL.has(location.pathname) ? "hub-global-content" : scope}
                  >
                    <NoBoundProjectBanner />
                    <RouteChunk key={location.pathname}>
                      {stewardEmpty ? <StewardEmptyState /> : <Outlet />}
                    </RouteChunk>
                  </div>
                </ModuleDemoProvider>
              </V2ToastProvider>
            ) : hosted === "classic" ? (
              <div
                className="view v3-classic"
                key={HUB_GLOBAL.has(location.pathname) ? "hub-global-content" : scope}
              >
                <NoBoundProjectBanner />
                <RouteChunk key={location.pathname}>
                  {stewardEmpty ? <StewardEmptyState /> : <Outlet />}
                </RouteChunk>
              </div>
            ) : (
              <div key={HUB_GLOBAL.has(location.pathname) ? "hub-global-content" : scope}>
                <NoBoundProjectBanner />
                <RouteChunk>
                  {stewardEmpty ? <StewardEmptyState /> : <Outlet />}
                </RouteChunk>
              </div>
            )}
          </HiddenNavContext.Provider>
        </main>

        {changingPassword && username && (
          <ChangePasswordDialog username={username} builtin={builtin} onClose={() => setChangingPassword(false)} />
        )}
        <CommandPalette open={paletteOpen} onClose={closePalette} agents={agents.data?.agents ?? []} pages={[...pages, ...projectCommands, ...help]}
          gate={native.some((item) => item.to === "/v3/gate")} />
        <GlossaryDialog />
        {touring && <Tour onClose={() => setTouring(false)} />}
      </ToastProvider>
    </div>
  );
}

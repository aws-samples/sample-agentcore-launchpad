import {
  Bot,
  BrainCircuit,
  ChartColumn,
  Cpu,
  Database,
  FlaskConical,
  Gauge,
  House,
  KeyRound,
  Layers,
  Link2,
  LibraryBig,
  ListChecks,
  MessagesSquare,
  Network,
  PlayCircle,
  Radar,
  ScrollText,
  Settings,
  ShieldCheck,
  Sparkles,
  SquareStack,
  Target,
  Users,
  Wallet,
  Workflow,
  type LucideIcon,
} from "lucide-react";

import type { NavMode } from "../lib/nav-mode";

/**
 * One sidebar entry. `v2: true` entries are native V2 pages under /v2; the
 * others are classic module routes, which render inside the V2 shell on its
 * light theme until the module is rebuilt natively.
 */
export interface V2NavItem {
  to: string;
  labelKey: string;
  icon: LucideIcon;
  v2?: boolean;
  /** only rendered for administrators */
  admin?: boolean;
  /** exact-match active state (the /v2 index) */
  end?: boolean;
  /** other path prefixes that belong to this entry (e.g. the classic create
   *  and edit flows of a module whose list is native) */
  also?: string[];
  /** also shown in the pared-down `business` sidebar mode (see lib/nav-mode.ts) */
  business?: true;
  /** glossary sentence shown as the entry's native tooltip, keeping the sidebar
   *  itself uncluttered */
  hintKey?: string;
}

export interface V2NavGroup {
  key: string;
  labelKey: string;
  items: V2NavItem[];
}

export const V2_NAV: V2NavGroup[] = [
  {
    key: "home",
    labelKey: "v2.nav.groupHome",
    items: [{ to: "/v2", labelKey: "v2.nav.home", icon: House, v2: true, end: true, business: true }],
  },
  {
    key: "build",
    labelKey: "v2.nav.groupBuild",
    items: [
      {
        to: "/v2/assistant",
        labelKey: "nav.assistant",
        icon: Sparkles,
        v2: true,
        also: ["/create/assistant"],
        business: true,
      },
      {
        to: "/v2/agents",
        labelKey: "nav.createAgent",
        icon: Bot,
        v2: true,
        also: ["/agents", "/create/studio"],
        business: true,
        hintKey: "glossary.agent",
      },
      { to: "/v2/registry", labelKey: "nav.registry", icon: SquareStack, v2: true, hintKey: "glossary.registry" },
      {
        to: "/v2/knowledge-bases",
        labelKey: "nav.knowledgeBases",
        icon: LibraryBig,
        v2: true,
        business: true,
        hintKey: "glossary.knowledgeBase",
      },
      // expert mode only (D7): the COA context layer this workspace is connected to
      { to: "/v2/context-layer", labelKey: "nav.contextLayer", icon: Network, v2: true },
    ],
  },
  {
    key: "run",
    labelKey: "v2.nav.groupRun",
    items: [
      { to: "/v2/chat", labelKey: "nav.chat", icon: MessagesSquare, v2: true, business: true },
      {
        to: "/v2/observability",
        labelKey: "nav.observability",
        icon: Gauge,
        v2: true,
        business: true,
        hintKey: "glossary.observability",
      },
      { to: "/v2/memory", labelKey: "nav.memory", icon: Layers, v2: true, hintKey: "glossary.memory" },
      {
        to: "/v2/governance",
        labelKey: "nav.governance",
        icon: ShieldCheck,
        v2: true,
        hintKey: "glossary.cedarPolicy",
      },
      { to: "/v2/my-connections", labelKey: "nav.myConnections", icon: Link2, v2: true },
    ],
  },
  {
    key: "eval",
    labelKey: "v2.nav.groupEval",
    items: [
      { to: "/v2/eval/insights", labelKey: "v2.nav.insights", icon: ChartColumn, v2: true },
      { to: "/v2/eval/data", labelKey: "v2.nav.dataCenter", icon: Database, v2: true, hintKey: "glossary.dataset" },
      { to: "/v2/eval/tasks", labelKey: "v2.nav.tasks", icon: ListChecks, v2: true },
      {
        to: "/v2/eval/online",
        labelKey: "v2.nav.online",
        icon: Radar,
        v2: true,
        hintKey: "glossary.onlineEvaluation",
      },
      {
        to: "/v2/eval/evaluators",
        labelKey: "v2.nav.evaluators",
        icon: Target,
        v2: true,
        hintKey: "glossary.evaluator",
      },
      {
        to: "/v2/eval/experiments",
        labelKey: "v2.nav.experiments",
        icon: FlaskConical,
        v2: true,
        hintKey: "glossary.experiment",
      },
      // Skill Lab evaluates and trains Skills against task sets — an evaluation surface
      { to: "/v2/skill-lab", labelKey: "nav.skillLab", icon: BrainCircuit, v2: true, hintKey: "glossary.skill" },
    ],
  },
  {
    key: "learn",
    labelKey: "v2.nav.groupLearn",
    items: [
      { to: "/v2/videos", labelKey: "nav.videos", icon: PlayCircle, v2: true, business: true },
    ],
  },
  {
    key: "admin",
    labelKey: "v2.nav.groupAdmin",
    items: [
      { to: "/v2/users", labelKey: "nav.users", icon: Users, v2: true, admin: true },
      {
        to: "/v2/workspaces",
        labelKey: "nav.workspaces",
        icon: Workflow,
        v2: true,
        admin: true,
        hintKey: "glossary.workspace",
      },
      { to: "/v2/connections", labelKey: "nav.connections", icon: KeyRound, v2: true, admin: true },
      { to: "/v2/models", labelKey: "v2.nav.models", icon: Cpu, v2: true, admin: true },
      { to: "/v2/costs", labelKey: "v2.nav.costs", icon: Wallet, v2: true, admin: true },
      { to: "/v2/announcements", labelKey: "nav.announcements", icon: ScrollText, v2: true, admin: true },
      { to: "/v2/video-management", labelKey: "videoManage.title", icon: Settings, v2: true, admin: true },
    ],
  },
];

/**
 * The sidebar a viewer sees: admin-only entries for administrators only, and in
 * `business` mode only the `business` entries. Groups left empty are dropped;
 * order is kept. A view filter only: every route stays reachable by URL.
 */
export function navGroupsFor(mode: NavMode, isAdmin: boolean): V2NavGroup[] {
  return V2_NAV.map((group) => ({
    ...group,
    items: group.items.filter(
      (item) => (!item.admin || isAdmin) && (mode === "expert" || item.business === true),
    ),
  })).filter((group) => group.items.length > 0);
}

export function isNavItemActive(item: V2NavItem, pathname: string, search: string): boolean {
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

/** The sidebar entry (module) a location belongs to, including its sub-pages. */
export function activeNavItem(pathname: string, search: string): V2NavItem | undefined {
  for (const group of V2_NAV) {
    const item = group.items.find((entry) => isNavItemActive(entry, pathname, search));
    if (item) return item;
  }
  return undefined;
}

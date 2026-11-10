import {
  Bot,
  ChartColumn,
  Cpu,
  Database,
  FlaskConical,
  BrainCircuit,
  Gauge,
  House,
  KeyRound,
  Layers,
  LibraryBig,
  ListChecks,
  MessagesSquare,
  PlayCircle,
  Radar,
  Scale,
  ShieldCheck,
  Sparkles,
  SquareStack,
  Target,
  Users,
  Workflow,
  type LucideIcon,
} from "lucide-react";

import type { Feature } from "../lib/capabilities";

/**
 * One sidebar entry. Every entry is a native V2 page under /v2 (`v2: true`);
 * `also` lists the token-styled agent flows that still live on their original
 * routes (`/agents/new`, `/agents/import`, `/agents/:id/edit`, `/create/studio`).
 */
export interface V2NavItem {
  to: string;
  labelKey: string;
  icon: LucideIcon;
  v2?: boolean;
  /** only rendered for administrators */
  admin?: boolean;
  /** only rendered when this deployment exposes the feature (omitted = always) */
  feature?: Feature;
  /** exact-match active state (the /v2 index) */
  end?: boolean;
  /** other path prefixes that belong to this entry (e.g. the agent flows that
   *  still render on their original routes next to the native list) */
  also?: string[];
  /** a count pill after the label: `registry_pending` = the workspace's PENDING_APPROVAL
   *  registry records, administrators only and hidden at 0 (N17-obs267) */
  badge?: "registry_pending";
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
    items: [
      { to: "/v2", labelKey: "v2.nav.home", icon: House, v2: true, end: true },
      // Architect assistant — under Overview (user decision 2026-10-26), its own entry as in
      // the Global console (the preset is installed from System agents; the page explains
      // when it is not running).
      {
        to: "/v2/assistant",
        labelKey: "nav.assistant",
        icon: Sparkles,
        v2: true,
        feature: "agents",
        also: ["/create/assistant"],
      },
    ],
  },
  {
    key: "build",
    labelKey: "v2.nav.groupBuild",
    items: [
      {
        to: "/v2/agents",
        labelKey: "nav.createAgent",
        icon: Bot,
        v2: true,
        feature: "agents",
        also: ["/agents", "/create/studio"],
      },
      {
        to: "/v2/registry",
        labelKey: "nav.registry",
        icon: SquareStack,
        v2: true,
        feature: "registry",
        badge: "registry_pending",
      },
      {
        to: "/v2/knowledge-bases",
        labelKey: "nav.knowledgeBases",
        icon: LibraryBig,
        v2: true,
        feature: "knowledge_bases",
      },
    ],
  },
  {
    key: "run",
    labelKey: "v2.nav.groupRun",
    items: [
      { to: "/v2/chat", labelKey: "nav.chat", icon: MessagesSquare, v2: true, feature: "chat" },
      { to: "/v2/memory", labelKey: "nav.memory", icon: Layers, v2: true, feature: "memory" },
      {
        to: "/v2/connections",
        labelKey: "nav.identity",
        icon: KeyRound,
        v2: true,
        feature: "identity",
      },
      {
        to: "/v2/governance",
        labelKey: "nav.gateway",
        icon: ShieldCheck,
        v2: true,
        feature: "gateway",
        also: ["/governance"],
      },
      // Policy — a top-level entry beside Gateway and Identity, as in the Global console:
      // policy engines, their policies, the simulator, decisions and the gateway attachments
      { to: "/v2/policy", labelKey: "nav.policy", icon: Scale, v2: true, feature: "policy" },
      { to: "/v2/observability", labelKey: "nav.observability", icon: Gauge, v2: true, feature: "observability" },
    ],
  },
  {
    key: "eval",
    labelKey: "v2.nav.groupEval",
    items: [
      { to: "/v2/eval/insights", labelKey: "v2.nav.insights", icon: ChartColumn, v2: true, feature: "evaluation" },
      { to: "/v2/eval/data", labelKey: "v2.nav.dataCenter", icon: Database, v2: true, feature: "evaluation" },
      // Evaluators right below the data center (user decision 2026-10-26)
      { to: "/v2/eval/evaluators", labelKey: "v2.nav.evaluators", icon: Target, v2: true, feature: "evaluation" },
      { to: "/v2/eval/tasks", labelKey: "v2.nav.tasks", icon: ListChecks, v2: true, feature: "evaluation" },
      { to: "/v2/eval/online", labelKey: "v2.nav.online", icon: Radar, v2: true, feature: "evaluation" },
      { to: "/v2/eval/experiments", labelKey: "v2.nav.experiments", icon: FlaskConical, v2: true, feature: "optimization" },
      // Skill Lab evaluates and trains Skills against task sets — an evaluation surface
      { to: "/v2/skill-lab", labelKey: "nav.skillLab", icon: BrainCircuit, v2: true, feature: "skill_lab" },
    ],
  },
  {
    key: "admin",
    labelKey: "v2.nav.groupAdmin",
    items: [
      {
        to: "/v2/workspaces",
        labelKey: "nav.workspaces",
        icon: Workflow,
        v2: true,
        admin: true,
        feature: "workspaces",
      },
      { to: "/v2/users", labelKey: "nav.users", icon: Users, v2: true, admin: true, feature: "users" },
      // D21: the model registry always exists (no feature flag). The entry lives in the
      // Configuration group (user decision 2026-10-21 — it briefly sat in the platform-services
      // group, N17-obs224). N17-obs203: providers are owner-bound — every member registers their
      // own — so the entry carries no `admin` flag; for a member the Configuration group therefore
      // renders with this single entry (workspaces / users stay administrators-only) and the page
      // itself hides the admin tabs (roles, workspaces, changes) from members.
      { to: "/v2/settings/model-providers", labelKey: "nav.modelProviders", icon: Cpu, v2: true },
    ],
  },
  // 学习 is the last group (user decision 2026-10-19, N15-obs14): one video entry for
  // everyone; administrators manage the directory on its 「管理」 tab (`?view=manage`).
  {
    key: "learn",
    labelKey: "v2.nav.groupLearn",
    items: [
      // hub-global tutorial video library; recordings play from permanent CDN URLs
      { to: "/v2/videos", labelKey: "nav.videos", icon: PlayCircle, v2: true, feature: "videos", also: ["/videos"] },
    ],
  },
];

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

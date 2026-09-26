import {
  Bot,
  BrainCircuit,
  ChartColumn,
  Database,
  FlaskConical,
  Gauge,
  House,
  Layers,
  LibraryBig,
  ListChecks,
  MessagesSquare,
  PlayCircle,
  Radar,
  ScrollText,
  Settings,
  ShieldCheck,
  Sparkles,
  SquareStack,
  Target,
  Users,
  Workflow,
  type LucideIcon,
} from "lucide-react";

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
    items: [{ to: "/v2", labelKey: "v2.nav.home", icon: House, v2: true, end: true }],
  },
  {
    key: "build",
    labelKey: "v2.nav.groupBuild",
    items: [
      { to: "/v2/assistant", labelKey: "nav.assistant", icon: Sparkles, v2: true, also: ["/create/assistant"] },
      {
        to: "/v2/agents",
        labelKey: "nav.createAgent",
        icon: Bot,
        v2: true,
        also: ["/agents", "/create/studio"],
      },
      { to: "/v2/registry", labelKey: "nav.registry", icon: SquareStack, v2: true },
      { to: "/v2/knowledge-bases", labelKey: "nav.knowledgeBases", icon: LibraryBig, v2: true },
      { to: "/v2/skill-lab", labelKey: "nav.skillLab", icon: BrainCircuit, v2: true },
    ],
  },
  {
    key: "run",
    labelKey: "v2.nav.groupRun",
    items: [
      { to: "/v2/chat", labelKey: "nav.chat", icon: MessagesSquare, v2: true },
      { to: "/v2/observability", labelKey: "nav.observability", icon: Gauge, v2: true },
      { to: "/v2/memory", labelKey: "nav.memory", icon: Layers, v2: true },
      { to: "/v2/governance", labelKey: "nav.governance", icon: ShieldCheck, v2: true },
    ],
  },
  {
    key: "eval",
    labelKey: "v2.nav.groupEval",
    items: [
      { to: "/v2/eval/data", labelKey: "v2.nav.dataCenter", icon: Database, v2: true },
      { to: "/v2/eval/tasks", labelKey: "v2.nav.tasks", icon: ListChecks, v2: true },
      { to: "/v2/eval/insights", labelKey: "v2.nav.insights", icon: ChartColumn, v2: true },
      { to: "/v2/eval/online", labelKey: "v2.nav.online", icon: Radar, v2: true },
      { to: "/v2/eval/evaluators", labelKey: "v2.nav.evaluators", icon: Target, v2: true },
      { to: "/v2/eval/experiments", labelKey: "v2.nav.experiments", icon: FlaskConical, v2: true },
    ],
  },
  {
    key: "learn",
    labelKey: "v2.nav.groupLearn",
    items: [
      { to: "/v2/videos", labelKey: "nav.videos", icon: PlayCircle, v2: true },
    ],
  },
  {
    key: "admin",
    labelKey: "v2.nav.groupAdmin",
    items: [
      { to: "/v2/users", labelKey: "nav.users", icon: Users, v2: true, admin: true },
      { to: "/v2/workspaces", labelKey: "nav.workspaces", icon: Workflow, v2: true, admin: true },
      { to: "/v2/announcements", labelKey: "nav.announcements", icon: ScrollText, v2: true, admin: true },
      { to: "/v2/video-management", labelKey: "videoManage.title", icon: Settings, v2: true, admin: true },
    ],
  },
];

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
  ScrollText,
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
      { to: "/create/assistant", labelKey: "nav.assistant", icon: Sparkles },
      { to: "/agents", labelKey: "nav.createAgent", icon: Bot },
      { to: "/registry", labelKey: "nav.registry", icon: SquareStack },
      { to: "/knowledge-bases", labelKey: "nav.knowledgeBases", icon: LibraryBig },
      { to: "/skill-lab", labelKey: "nav.skillLab", icon: BrainCircuit },
    ],
  },
  {
    key: "run",
    labelKey: "v2.nav.groupRun",
    items: [
      { to: "/chat", labelKey: "nav.chat", icon: MessagesSquare },
      { to: "/observability", labelKey: "nav.observability", icon: Gauge },
      { to: "/memory", labelKey: "nav.memory", icon: Layers },
      { to: "/governance", labelKey: "nav.governance", icon: ShieldCheck },
    ],
  },
  {
    key: "eval",
    labelKey: "v2.nav.groupEval",
    items: [
      { to: "/v2/eval/data", labelKey: "v2.nav.dataCenter", icon: Database, v2: true },
      { to: "/v2/eval/tasks", labelKey: "v2.nav.tasks", icon: ListChecks, v2: true },
      { to: "/v2/eval/insights", labelKey: "v2.nav.insights", icon: ChartColumn, v2: true },
      { to: "/v2/eval/evaluators", labelKey: "v2.nav.evaluators", icon: Target, v2: true },
      { to: "/evaluation?view=experiment", labelKey: "v2.nav.experiments", icon: FlaskConical },
    ],
  },
  {
    key: "admin",
    labelKey: "v2.nav.groupAdmin",
    items: [
      { to: "/users", labelKey: "nav.users", icon: Users, admin: true },
      { to: "/workspaces", labelKey: "nav.workspaces", icon: Workflow, admin: true },
      { to: "/announcements", labelKey: "nav.announcements", icon: ScrollText, admin: true },
      { to: "/videos", labelKey: "nav.videos", icon: PlayCircle },
    ],
  },
];

export interface NavEntry {
  idx: string;
  to: string;
  labelKey: string;
  end?: boolean;
}

export const NAV_ENTRIES: NavEntry[] = [
  { idx: "01", to: "/", labelKey: "nav.overview", end: true },
  { idx: "02", to: "/create", labelKey: "nav.createAgent" },
  { idx: "03", to: "/registry", labelKey: "nav.registry" },
  { idx: "04", to: "/knowledge-bases", labelKey: "nav.knowledgeBases" },
  { idx: "05", to: "/memory", labelKey: "nav.memory" },
  { idx: "06", to: "/chat", labelKey: "nav.chat" },
  { idx: "07", to: "/observability", labelKey: "nav.observability" },
  { idx: "08", to: "/evaluation", labelKey: "nav.evaluation" },
  { idx: "09", to: "/governance", labelKey: "nav.governance" },
];

export const PLATFORM_COUNT = 6;

/** Admin-only entries: rendered by the sidebar only for an administrator. */
export const ADMIN_NAV_ENTRIES: NavEntry[] = [
  { idx: "10", to: "/users", labelKey: "nav.users" },
];

/** Every routable entry, for breadcrumb resolution. */
export const ALL_NAV_ENTRIES: NavEntry[] = [...NAV_ENTRIES, ...ADMIN_NAV_ENTRIES];

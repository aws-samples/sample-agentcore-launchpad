export interface NavEntry {
  idx: string;
  to: string;
  labelKey: string;
  end?: boolean;
  /** Every action on this page needs an administrator (see backend route_policy). */
  adminOnly?: boolean;
}

export const NAV_ENTRIES: NavEntry[] = [
  { idx: "01", to: "/", labelKey: "nav.overview", end: true },
  // members reach it too since 2026-08-07: reads are open, and the mutating
  // actions are gated per user by agent-management permissions (auth `can()`)
  { idx: "02", to: "/create", labelKey: "nav.createAgent" },
  { idx: "03", to: "/registry", labelKey: "nav.registry" },
  { idx: "04", to: "/knowledge-bases", labelKey: "nav.knowledgeBases" },
  { idx: "05", to: "/memory", labelKey: "nav.memory" },
  { idx: "06", to: "/chat", labelKey: "nav.chat" },
  { idx: "07", to: "/observability", labelKey: "nav.observability" },
  { idx: "08", to: "/evaluation", labelKey: "nav.evaluation" },
  { idx: "09", to: "/skill-lab", labelKey: "nav.skillLab" },
  { idx: "10", to: "/governance", labelKey: "nav.governance" },
];

export const PLATFORM_COUNT = 6;

/**
 * Admin-only entries: rendered by the sidebar only for an administrator.
 *
 * Distinct from `adminOnly` on a NAV_ENTRY — these are whole modules that only
 * exist for administrators, whereas an `adminOnly` platform entry keeps its place
 * in the numbered flow (dropping `/create` from the list would renumber the
 * console for members).
 */
export const ADMIN_NAV_ENTRIES: NavEntry[] = [
  { idx: "11", to: "/users", labelKey: "nav.users" },
  { idx: "12", to: "/workspaces", labelKey: "nav.workspaces" },
];

/** Every routable entry, for breadcrumb resolution. */
export const ALL_NAV_ENTRIES: NavEntry[] = [...NAV_ENTRIES, ...ADMIN_NAV_ENTRIES];

/**
 * Every path the router in `App.tsx` matches, in react-router pattern form.
 * Keep it in step with the `<Route>` table there: the Shell's breadcrumb falls
 * back to `nav.notFound` when the current pathname matches none of these, which
 * is exactly when the catch-all route renders the not-found view.
 */
export const ROUTE_PATHS: string[] = [
  "/",
  "/create",
  "/create/studio",
  "/registry",
  "/knowledge-bases",
  "/memory",
  "/chat",
  "/observability",
  "/evaluation",
  "/skill-lab",
  "/governance",
  "/users",
  "/workspaces",
];

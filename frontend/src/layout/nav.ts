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
  // The architect assistant gets its own slot: it is the guided entrance to agent
  // creation, not a sub-view of the management page.
  { idx: "02", to: "/create/assistant", labelKey: "nav.assistant" },
  // members reach it too since 2026-08-07: reads are open, and the mutating
  // actions are gated per user by agent-management permissions (auth `can()`)
  { idx: "03", to: "/agents", labelKey: "nav.createAgent" },
  { idx: "04", to: "/registry", labelKey: "nav.registry" },
  { idx: "05", to: "/knowledge-bases", labelKey: "nav.knowledgeBases" },
  { idx: "06", to: "/memory", labelKey: "nav.memory" },
  { idx: "07", to: "/chat", labelKey: "nav.chat" },
  { idx: "08", to: "/observability", labelKey: "nav.observability" },
  { idx: "09", to: "/evaluation", labelKey: "nav.evaluation" },
  { idx: "10", to: "/skill-lab", labelKey: "nav.skillLab" },
  { idx: "11", to: "/governance", labelKey: "nav.governance" },
];

export const PLATFORM_COUNT = 7;

/**
 * Admin-only entries: rendered by the sidebar only for an administrator.
 *
 * Distinct from `adminOnly` on a NAV_ENTRY — these are whole modules that only
 * exist for administrators, whereas an `adminOnly` platform entry keeps its place
 * in the numbered flow (dropping `/agents` from the list would renumber the
 * console for members).
 */
export const ADMIN_NAV_ENTRIES: NavEntry[] = [
  { idx: "12", to: "/users", labelKey: "nav.users" },
  { idx: "13", to: "/workspaces", labelKey: "nav.workspaces" },
  { idx: "◉", to: "/announcements", labelKey: "nav.announcements", adminOnly: true },
];

/** Learning resources sit outside the console's numbered operation sequence. */
export const LEARN_NAV_ENTRIES: NavEntry[] = [
  { idx: "▶", to: "/videos", labelKey: "nav.videos" },
];

/** Every routable entry, for breadcrumb resolution. */
export const ALL_NAV_ENTRIES: NavEntry[] = [
  ...NAV_ENTRIES, ...ADMIN_NAV_ENTRIES, ...LEARN_NAV_ENTRIES,
];

/**
 * The entry a pathname belongs to: the LONGEST `to` that prefixes it, so
 * `/create/assistant` resolves to the assistant entry and `/create/studio` to
 * Agent management. `null` for the index route and for unrouted paths.
 */
export function navEntryFor(pathname: string): NavEntry | null {
  let best: NavEntry | null = null;
  for (const entry of ALL_NAV_ENTRIES) {
    if (entry.to === "/") continue;
    if (pathname === entry.to || pathname.startsWith(`${entry.to}/`)) {
      if (!best || entry.to.length > best.to.length) best = entry;
    }
  }
  return best;
}

/**
 * Every path the router in `App.tsx` matches, in react-router pattern form.
 * Keep it in step with the `<Route>` table there: the Shell's breadcrumb falls
 * back to `nav.notFound` when the current pathname matches none of these, which
 * is exactly when the catch-all route renders the not-found view.
 */
export const ROUTE_PATHS: string[] = [
  "/",
  "/agents",
  "/agents/new",
  "/agents/import",
  "/agents/:agentId",
  "/agents/:agentId/edit",
  "/create",
  "/create/studio",
  "/create/assistant",
  "/registry",
  "/knowledge-bases",
  "/memory",
  "/chat",
  "/observability",
  "/evaluation",
  "/skill-lab",
  "/governance",
  "/users",
  "/announcements",
  "/workspaces",
  "/videos",
];

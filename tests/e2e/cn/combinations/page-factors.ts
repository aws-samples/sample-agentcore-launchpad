/**
 * Control factors of the six high-density V2 list pages for the page-pairwise suite
 * (round 16, r16-n step 2). r16-m's control inventory (`frontend/src/v2/__inventory__/
 * controls.json`) had not landed when this was written, so the factors are DERIVED FROM THE
 * SOURCE; every table cites the file it mirrors. The pure part (tables, constraints, the URL
 * each row must show) is unit-tested; the Playwright driver lives in p1-page-pairwise.spec.ts.
 *
 * Common factors on every page: `role` (admin / member — the x16n-member fixture) and `vis`
 * (the default workspace's resource_visibility, shared / isolated — admin-toggled, restored
 * at the end). `page` is "1" or "last" (the Pager's last page), `q` a search needle:
 * "" (none), "x16n" (hits the fixtures), "zzz" (matches nothing).
 */
import type { Combo, Constraint, Factor } from "./pairwise";

export type PageKey = "tasks" | "data" | "scorecard" | "agents" | "registry" | "users";
export const PAGES: PageKey[] = ["tasks", "data", "scorecard", "agents", "registry", "users"];

const COMMON: Factor[] = [
  { name: "role", values: ["admin", "member"] },
  { name: "vis", values: ["shared", "isolated"] },
  { name: "page", values: ["1", "last"] },
  { name: "q", values: ["", "x16n", "zzz"] },
];

/** pages/tasks/TaskList.tsx (`?tab=all`): four FilterSelects + search, Pager 12; the
 *  `agents` tab (AgentScoreList.tsx) has search + Pager 12 only. Tabs ride in `?tab=`. */
const TASKS: Factor[] = [
  { name: "tab", values: ["all", "agents"] },
  { name: "status", values: ["na", "all", "completed", "running", "failed", "stopped"] },
  { name: "source", values: ["na", "all", "dataset", "window", "sessions", "live"] },
  { name: "mode", values: ["na", "all", "evaluators", "insights"] },
  { name: "agent", values: ["na", "all", "fixture"] },
  ...COMMON,
];
/** pages/data/Traces.tsx (`?tab=traces&range=`): range (URL) · agent · status · search ·
 *  row selection (→ "add to dataset" count) · Pager 15; pages/data/Datasets.tsx: kind ·
 *  origin · search · Pager 12. */
const DATA: Factor[] = [
  { name: "tab", values: ["traces", "datasets"] },
  { name: "range", values: ["na", "1h", "6h", "24h", "7d"] },
  { name: "agent", values: ["na", "all", "fixture"] },
  { name: "status", values: ["na", "all", "ok", "error"] },
  { name: "select", values: ["na", "none", "one", "pageAll"] },
  { name: "kind", values: ["na", "all", "predefined", "simulated"] },
  { name: "origin", values: ["na", "all", "manual", "trace"] },
  ...COMMON,
];
/** pages/tasks/AgentScorecard.tsx (`?view=agent&key=&bench=`): benchmark tabs (URL `bench`),
 *  the "only this benchmark" toggle, run picks (A/B → compare), Pager 10. No search. */
const SCORECARD: Factor[] = [
  { name: "bench", values: ["first", "second"] },
  { name: "onlyBench", values: ["on", "off"] },
  { name: "pick", values: ["none", "one", "two"] },
  ...COMMON.filter((f) => f.name !== "q"),
];
/** pages/Agents.tsx list: method · status · search · Pager 12; nothing in the URL. */
const AGENTS: Factor[] = [
  { name: "method", values: ["all", "harness", "zip_runtime", "byoc"] },
  { name: "status", values: ["all", "active", "deploying", "failed"] },
  ...COMMON,
];
/** pages/registry/RecordList.tsx (`?type=` on the records tab): tab (records / discoverable),
 *  type (URL), status (records only) · search · Pager 12. Registry reads stay shared. */
const REGISTRY: Factor[] = [
  { name: "tab", values: ["records", "discoverable"] },
  { name: "type", values: ["all", "A2A", "MCP", "AGENT_SKILLS"] },
  { name: "status", values: ["na", "all", "APPROVED", "DRAFT", "PENDING_APPROVAL"] },
  ...COMMON,
];
/** pages/users/UserList.tsx: `?status=&q=&page=` — the backend pages (25). Admin only: a
 *  member must get a readable refusal, never a broken page; visibility does not apply. */
const USERS: Factor[] = [
  { name: "status", values: ["na", "all", "pending", "active", "expired", "disabled"] },
  ...COMMON.filter((f) => f.name !== "vis"),
];

export const PAGE_FACTORS: Record<PageKey, Factor[]> = { tasks: TASKS, data: DATA, scorecard: SCORECARD, agents: AGENTS, registry: REGISTRY, users: USERS };

type Maybe = boolean | undefined;
const iff = (p: Combo, name: string, cond: Maybe): boolean => {
  if (p[name] === undefined || cond === undefined) return true;
  return (p[name] === "na") === !cond;
};

export const PAGE_CONSTRAINTS: Record<PageKey, Constraint> = {
  tasks: (p) => {
    const all: Maybe = p.tab === undefined ? undefined : p.tab === "all";
    return ["status", "source", "mode", "agent"].every((f) => iff(p, f, all));
  },
  data: (p) => {
    const traces: Maybe = p.tab === undefined ? undefined : p.tab === "traces";
    const datasets: Maybe = traces === undefined ? undefined : !traces;
    return ["range", "agent", "status", "select"].every((f) => iff(p, f, traces)) && ["kind", "origin"].every((f) => iff(p, f, datasets));
  },
  scorecard: () => true,
  agents: () => true,
  registry: (p) => iff(p, "status", p.tab === undefined ? undefined : p.tab === "records"),
  users: (p) => {
    const admin: Maybe = p.role === undefined ? undefined : p.role === "admin";
    // a member sees the refusal only: no filter / page / search to drive
    if (!iff(p, "status", admin)) return false;
    if (p.role === "member" && p.q !== undefined && p.q !== "") return false;
    if (p.role === "member" && p.page !== undefined && p.page !== "1") return false;
    return true;
  },
};

/** The route of a page (every factor that the URL carries is applied by the driver). */
export function pagePath(page: PageKey, combo: Combo, ctx: { agentKey?: string } = {}): string {
  switch (page) {
    case "tasks":
      return combo.tab === "all" ? "/v2/eval/tasks?tab=all" : "/v2/eval/tasks";
    case "data":
      return combo.tab === "traces" ? `/v2/eval/data?tab=traces${combo.range && combo.range !== "na" ? `&range=${combo.range}` : ""}` : "/v2/eval/data?tab=datasets";
    case "scorecard":
      return `/v2/eval/tasks?view=agent&key=${encodeURIComponent(ctx.agentKey ?? "")}`;
    case "agents":
      return "/v2/agents";
    case "registry":
      return combo.tab === "discoverable" ? "/v2/registry?view=discoverable" : "/v2/registry";
    case "users":
      return "/v2/users";
  }
}

/** Query parameters the page PROMISES to reflect for a row (the ones read back with
 *  useSearchParams); anything else living in React state is deliberately not asserted. */
export function promisedParams(page: PageKey, combo: Combo): Record<string, string | null> {
  switch (page) {
    case "tasks":
      return combo.tab === "all" ? { tab: "all" } : { tab: null };
    case "data":
      return combo.tab === "traces" ? { tab: "traces", ...(combo.range !== "na" ? { range: combo.range } : {}) } : { tab: "datasets" };
    case "scorecard":
      return { view: "agent" };
    case "agents":
      return {};
    case "registry":
      if (combo.tab === "discoverable") return { view: "discoverable" };
      return combo.type !== "all" ? { type: combo.type } : {};
    case "users":
      if (combo.role !== "admin") return {};
      return {
        ...(combo.status !== "all" && combo.status !== "na" ? { status: combo.status } : { status: null }),
        ...(combo.q ? { q: combo.q } : { q: null }),
      };
  }
}

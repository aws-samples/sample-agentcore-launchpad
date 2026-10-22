// The URL contract of 评估任务 (`/v2/eval/tasks`) that more than one place reads: which
// list tab a search string opens, which create form `?view=new` shows, and where the
// URLs of the retired 在线评估 page (`/v2/eval/online`, the classic
// `/evaluation?view=online`) land now that its configs are the 持续评估 tab. Pure, so
// vitest covers it.

export type TasksTab = "agents" | "runs" | "online";
/** The two create forms: a one-off batch run, or a continuous online evaluation config. */
export type NewStrategy = "history" | "continuous";

const TASKS_PATH = "/v2/eval/tasks";

/** The list tab: none → 按 Agent; `runs` (and the pre-merge `all`, still in old links and
 *  saved Back state) → 一次性评估; `online` → 持续评估. */
export function parseTasksTab(params: URLSearchParams): TasksTab {
  const tab = params.get("tab");
  if (tab === "online") return "online";
  return tab === "runs" || tab === "all" ? "runs" : "agents";
}

/** The search params a tab writes (按 Agent is the bare page). */
export function tasksTabParams(tab: TasksTab): Record<string, string> {
  return tab === "agents" ? {} : { tab };
}

/** Which create form `?view=new` shows: a copy keeps the kind of its source
 *  (`from=run:` / `from=online:`), otherwise `strategy=continuous` picks the online form. */
export function newStrategy(params: URLSearchParams): NewStrategy {
  const from = params.get("from");
  if (from) return from.startsWith("online:") ? "continuous" : "history";
  return params.get("strategy") === "continuous" ? "continuous" : "history";
}

/** The params of a create form. Switching forms carries only the agent: a copy is of one
 *  kind, and the other hand-offs (dataset, evaluators) belong to the one-off form. */
export function newTaskParams(strategy: NewStrategy, agentId?: string | null): Record<string, string> {
  return {
    view: "new",
    ...(strategy === "continuous" ? { strategy: "continuous" } : {}),
    ...(agentId ? { agent: agentId } : {}),
  };
}

/** An old `/v2/eval/online` query → its 评估任务 URL: the list, new, detail or edit. */
export function onlineToTasksUrl(search: string): string {
  const params = new URLSearchParams(search);
  const view = params.get("view");
  const id = params.get("id");
  const next =
    view === "new"
      ? newTaskParams("continuous")
      : (view === "detail" || view === "edit") && id
        ? { view, kind: "online", id }
        : tasksTabParams("online");
  return `${TASKS_PATH}?${new URLSearchParams(next).toString()}`;
}

/** A classic `/evaluation?view=online[&oe=new|<id>]` query → the same targets, in one hop. */
export function classicOnlineToTasksUrl(search: string): string {
  const oe = new URLSearchParams(search).get("oe");
  const old: Record<string, string> = oe === "new" ? { view: "new" } : oe ? { view: "detail", id: oe } : {};
  return onlineToTasksUrl(new URLSearchParams(old).toString());
}

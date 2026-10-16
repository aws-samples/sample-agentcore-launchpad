import { useSearchParams } from "react-router-dom";

import { RunCompare } from "./tasks/RunCompare";
import { TaskDetail } from "./tasks/TaskDetail";
import { TaskList } from "./tasks/TaskList";
import { TaskWizard } from "./tasks/TaskWizard";

/**
 * 评估任务 — batch evaluation runs (history strategy) and online evaluation
 * configs (continuous strategy) as one task list. Sub-pages are `?view=`
 * states: `new` (2-step wizard, `from=` to copy a task, `agent=` / `dataset=` /
 * `evaluators=` to preselect them), `detail` (`kind=run|online&id=`) and `compare`
 * (`a=<runId>&b=<runId>`: two finished evaluator runs side by side).
 */
export function V2Tasks() {
  const [params] = useSearchParams();
  const view = params.get("view");
  const id = params.get("id");
  const kind = params.get("kind") === "online" ? "online" : "run";
  if (view === "new") {
    const key = ["from", "agent", "dataset", "evaluators"].map((k) => params.get(k)).join(":");
    return <TaskWizard key={key} />;
  }
  if (view === "detail" && id) return <TaskDetail key={`${kind}:${id}`} kind={kind} id={id} />;
  const a = params.get("a");
  const b = params.get("b");
  if (view === "compare" && a && b) return <RunCompare key={`${a}:${b}`} a={a} b={b} />;
  return <TaskList />;
}

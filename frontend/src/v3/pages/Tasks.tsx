import "./tasks.css";

import { useSearchParams } from "react-router-dom";

import { newStrategy } from "../../v2/pages/tasks/urls";
import { OnlineEditor } from "./online/Editor";
import { TaskDetail } from "./tasks/Detail";
import { TaskList } from "./tasks/List";
import { TaskWizard } from "./tasks/Wizard";

/**
 * 评估任务 (V3) — batch evaluation runs and online evaluation configs as one task
 * list, led by what failed and what is moving. Same `?view=` states as V2:
 * `new` — the run strategy decides the form: the run wizard (`from=run:<id>` to copy,
 * `agent=` / `dataset=` / `evaluators=` to preselect) or, with `strategy=continuous` or
 * `from=online:<id>`, the online evaluation form (`agent=`) — and `detail`
 * (`kind=run|online&id=`).
 */
export function V3Tasks() {
  const [params] = useSearchParams();
  const view = params.get("view");
  const id = params.get("id");
  const kind = params.get("kind") === "online" ? "online" : "run";
  if (view === "new") {
    const key = ["from", "agent", "dataset", "evaluators"].map((k) => params.get(k)).join(":");
    if (newStrategy(params) === "continuous") {
      const from = params.get("from");
      const seedFrom = from?.startsWith("online:") ? from.slice("online:".length) : null;
      return <OnlineEditor key={`new:${key}`} id={null} seedFrom={seedFrom} />;
    }
    return <TaskWizard key={key} />;
  }
  if (view === "detail" && id) return <TaskDetail key={`${kind}:${id}`} kind={kind} id={id} />;
  return <TaskList />;
}

import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { PageHeader, SubTabs } from "../ui";
import { OnlineDetail } from "./online/OnlineDetail";
import { OnlineEditor } from "./online/OnlineEditor";
import { OnlineList } from "./online/OnlineList";
import { AgentScorecard } from "./tasks/AgentScorecard";
import { AgentScoreList } from "./tasks/AgentScoreList";
import { RunCompare } from "./tasks/RunCompare";
import { TaskDetail } from "./tasks/TaskDetail";
import { TaskList } from "./tasks/TaskList";
import { TaskWizard } from "./tasks/TaskWizard";
import { newStrategy, parseTasksTab, type TasksTab, tasksTabParams } from "./tasks/urls";

const TAB_DESC: Record<TasksTab, string> = {
  agents: "v2.scorecard.desc",
  runs: "v2.tasks.desc",
  online: "v2.online.desc",
};

/**
 * 评估任务 — every evaluation, by how it runs: once (batch evaluation runs) or
 * continuously (online evaluation configs, the former 在线评估 page). Three list tabs:
 * 按 Agent (the default, no param — each agent's scores across its evaluations),
 * 一次性评估 (`tab=runs`; the pre-merge `tab=all` reads the same) and 持续评估
 * (`tab=online`, every online config). Sub-pages are `?view=` states:
 * - `new`: a strategy switch over two forms — the 2-step run wizard (`from=run:<id>` to
 *   copy a run, `agent=` / `dataset=` / `evaluators=` to preselect them) or, with
 *   `strategy=continuous` or `from=online:<id>`, the online config form (`agent=`);
 * - `detail` (`kind=run|online&id=`) and `edit` (`kind=online&id=`, agent-owned configs);
 * - `compare` (`a=<runId>&b=<runId>`: two finished evaluator runs side by side) and
 *   `agent` (`key=<agentKey>&bench=<benchmarkKey>`: one agent's scorecard).
 */
export function V2Tasks() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const view = params.get("view");
  const id = params.get("id");
  const online = params.get("kind") === "online";
  if (view === "new") {
    const key = ["from", "agent", "dataset", "evaluators"].map((k) => params.get(k)).join(":");
    if (newStrategy(params) === "continuous") {
      const from = params.get("from");
      const seedFrom = from?.startsWith("online:") ? from.slice("online:".length) : null;
      return <OnlineEditor key={`new:${key}`} id={null} seedFrom={seedFrom} />;
    }
    return <TaskWizard key={key} />;
  }
  if (view === "detail" && id) return online ? <OnlineDetail key={id} id={id} /> : <TaskDetail key={id} id={id} />;
  if (view === "edit" && id && online) return <OnlineEditor key={`edit:${id}`} id={id} />;
  const a = params.get("a");
  const b = params.get("b");
  if (view === "compare" && a && b) return <RunCompare key={`${a}:${b}`} a={a} b={b} />;
  const agentKey = params.get("key");
  if (view === "agent" && agentKey) return <AgentScorecard key={agentKey} agentKey={agentKey} />;

  const tab = parseTasksTab(params);
  return (
    <>
      <PageHeader
        title={t("v2.tasks.title")}
        tabs={
          <div data-testid="v2-scorecard-tabs">
            <SubTabs
              value={tab}
              tabs={[
                { value: "agents", label: t("v2.scorecard.tabs.agents") },
                { value: "runs", label: t("v2.scorecard.tabs.runs") },
                { value: "online", label: t("v2.scorecard.tabs.online"), title: t("glossary.onlineEvaluation") },
              ]}
              onChange={(next) => setParams(tasksTabParams(next))}
            />
          </div>
        }
        desc={t(TAB_DESC[tab])}
      />
      <div className="v2-tasks-list">
        {tab === "runs" ? <TaskList /> : tab === "online" ? <OnlineList /> : <AgentScoreList />}
      </div>
    </>
  );
}

import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { PageHeader, SubTabs } from "../ui";
import { AgentScorecard } from "./tasks/AgentScorecard";
import { AgentScoreList } from "./tasks/AgentScoreList";
import { RunCompare } from "./tasks/RunCompare";
import { TaskDetail } from "./tasks/TaskDetail";
import { TaskList } from "./tasks/TaskList";
import { TaskWizard } from "./tasks/TaskWizard";

type TasksTab = "agents" | "all";

/**
 * 评估任务 — batch evaluation runs (history strategy) and online evaluation
 * configs (continuous strategy). Two list tabs: 按 Agent (the default, no param — each
 * agent's scores across its evaluations) and 全部任务 (`tab=all`, every task). Sub-pages
 * are `?view=` states: `new` (2-step wizard, `from=` to copy a task, `agent=` /
 * `dataset=` / `evaluators=` to preselect them), `detail` (`kind=run|online&id=`),
 * `compare` (`a=<runId>&b=<runId>`: two finished evaluator runs side by side) and
 * `agent` (`key=<agentKey>&bench=<benchmarkKey>`: one agent's scorecard).
 */
export function V2Tasks() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
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
  const agentKey = params.get("key");
  if (view === "agent" && agentKey) return <AgentScorecard key={agentKey} agentKey={agentKey} />;

  const tab: TasksTab = params.get("tab") === "all" ? "all" : "agents";
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
                { value: "all", label: t("v2.scorecard.tabs.all") },
              ]}
              onChange={(next) => setParams(next === "all" ? { tab: "all" } : {})}
            />
          </div>
        }
        desc={tab === "all" ? t("v2.tasks.desc") : t("v2.scorecard.desc")}
      />
      <div className="v2-tasks-list">{tab === "all" ? <TaskList /> : <AgentScoreList />}</div>
    </>
  );
}

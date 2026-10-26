import "./tasks.css";

import { Plus } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { newStrategy, parseTasksTab, type TasksTab, tasksTabParams } from "../../v2/pages/tasks/urls";
import { permReason } from "../reasons";
import { Btn, Filters, PageHead } from "../ui";
import { OnlineDetail } from "./online/Detail";
import { OnlineEditor } from "./online/Editor";
import { OnlineList } from "./online/List";
import { TaskDetail } from "./tasks/Detail";
import { RunList } from "./tasks/List";
import { strategyUrl } from "./tasks/model";
import { TaskWizard } from "./tasks/Wizard";

/** The list tabs this page has; the bare URL opens the first. */
type Tab = Exclude<TasksTab, "agents">;
const TABS: Tab[] = ["runs", "online"];
const TAB_DESC: Record<Tab, string> = { runs: "v2.tasks.desc", online: "v2.online.desc" };

/** V2's tab parse, until 按 Agent (its default) is a tab here too. */
const tabOf = (params: URLSearchParams): Tab => {
  const tab = parseTasksTab(params);
  return tab === "agents" ? "runs" : tab;
};

/**
 * 评估任务 (V3) — every evaluation, by how it runs, with V2's URL contract
 * (`v2/pages/tasks/urls.ts`): 一次性评估 (the bare page, `tab=runs`; the pre-merge
 * `tab=all` reads the same) and 持续评估 (`tab=online`, every online config — the
 * former `/v3/online` page). Sub-pages are `?view=` states:
 * - `new` — the run strategy decides the form: the run wizard (`from=run:<id>` to copy,
 *   `agent=` / `dataset=` / `evaluators=` to preselect) or, with `strategy=continuous`
 *   or `from=online:<id>`, the online evaluation form (`agent=`);
 * - `detail` (`kind=run|online&id=`) and `edit` (`kind=online&id=`, agent-owned configs).
 */
export function V3Tasks() {
  const [params] = useSearchParams();
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
  return <TaskTabs tab={tabOf(params)} />;
}

function TaskTabs({ tab }: { tab: Tab }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [, setParams] = useSearchParams();
  const { can } = useAuth();
  const mayRun = can("eval.run");
  return (
    <div className="v3-task-page" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.tasks.eyebrow")}
        title={t("v3.tasks.title")}
        sub={t(TAB_DESC[tab])}
        end={
          <Btn kind="primary" disabled={!mayRun} disabledReason={permReason(t, "eval.run")}
            onClick={() => navigate(strategyUrl(tab === "online" ? "continuous" : "history"))}>
            <Plus size={14} /> {t("v2.tasks.new")}
          </Btn>
        }
      />
      <div data-testid="v3-tasks-tabs">
        <Filters
          value={tab}
          onChange={(next) => setParams(tasksTabParams(next))}
          options={TABS.map((value) => ({ value, label: t(`v2.scorecard.tabs.${value}`) }))}
        />
      </div>
      {tab === "online" ? <OnlineList /> : <RunList />}
    </div>
  );
}

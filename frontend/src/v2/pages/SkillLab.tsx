import { useTranslation } from "react-i18next";
import { Navigate, useSearchParams } from "react-router-dom";

import { api } from "../../lib/api";
import { useLoad } from "../hooks";
import { PageHeader, SubTabs } from "../ui";
import { ProvisionAlert } from "./skilllab/common";
import { EvalDetail } from "./skilllab/EvalDetail";
import { EvalWizard } from "./skilllab/EvalWizard";
import { JobList } from "./skilllab/JobList";
import "./skilllab/skilllab.css";
import { type Tab, TABS } from "./skilllab/state";
import { TaskgenDetail } from "./skilllab/TaskgenDetail";
import { TaskgenList } from "./skilllab/TaskgenList";
import { TaskgenWizard } from "./skilllab/TaskgenWizard";
import { TasksetDetail } from "./skilllab/TasksetDetail";
import { TasksetEditor } from "./skilllab/TasksetEditor";
import { TasksetList } from "./skilllab/TasksetList";
import { TrainDetail } from "./skilllab/TrainDetail";
import { TrainWizard } from "./skilllab/TrainWizard";

/**
 * 技能实验室 — evaluate and optimize Agent Skills against owned task sets.
 * Tabs: task sets (with the AI generation jobs below them), evaluation,
 * optimization. Sub-pages are `?tab=<tab>&view=new|edit|detail[&id=]` states
 * (plus `tab=tasksets&view=gen-new|gen&id=` for AI generation); the new-job
 * wizards also take `record=` (Registry deep link) and `taskset=` presets.
 */
export function V2SkillLab() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab: Tab = (TABS as string[]).includes(params.get("tab") ?? "") ? (params.get("tab") as Tab) : "tasksets";
  const view = params.get("view");
  const id = params.get("id");
  // provisioning only changes when someone runs bootstrap: one read per visit;
  // unreachable = null, and the views degrade on their own
  const statusLoad = useLoad(() => api.skillLabStatus().catch(() => null), "skill-lab-status");
  const status = statusLoad.data;

  // the former AI-generation tab now lives inside the task-set tab
  if (params.get("tab") === "taskgen") {
    const next = new URLSearchParams({ tab: "tasksets" });
    if (view === "new") next.set("view", "gen-new");
    else if (view === "detail" && id) {
      next.set("view", "gen");
      next.set("id", id);
    }
    return <Navigate to={`/v2/skill-lab?${next.toString()}`} replace />;
  }
  if (tab === "tasksets") {
    if (view === "gen-new") return <TaskgenWizard status={status} />;
    if (view === "gen" && id) return <TaskgenDetail key={id} id={id} />;
    if (view === "new") return <TasksetEditor key="new" id={null} />;
    if (view === "edit" && id) return <TasksetEditor key={`edit:${id}`} id={id} />;
    if (view === "detail" && id) return <TasksetDetail key={id} id={id} />;
  }
  if (tab === "eval") {
    if (view === "new") return <EvalWizard key={`${params.get("record")}:${params.get("taskset")}`} status={status} />;
    if (view === "detail" && id) return <EvalDetail key={id} id={id} />;
  }
  if (tab === "train") {
    if (view === "new") return <TrainWizard key={`${params.get("record")}:${params.get("taskset")}`} status={status} />;
    if (view === "detail" && id) return <TrainDetail key={id} id={id} />;
  }

  return (
    <>
      <PageHeader
        title={t("skillLab.title")}
        desc={t("skillLab.meta")}
        tabs={
          <SubTabs
            value={tab}
            onChange={(next) => setParams({ tab: next })}
            tabs={TABS.map((value) => ({ value, label: t(`v2.skillLab.tab.${value}`) }))}
          />
        }
      />
      <ProvisionAlert status={status} />
      {tab === "tasksets" && (
        <>
          <TasksetList />
          <TaskgenList />
        </>
      )}
      {tab === "eval" && <JobList key="eval" type="eval" />}
      {tab === "train" && <JobList key="train" type="train" />}
    </>
  );
}

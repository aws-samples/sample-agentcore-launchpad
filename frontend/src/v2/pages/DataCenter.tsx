import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { PageHeader, SubTabs } from "../ui";
import { DatasetDetail, DatasetEditor, DatasetsTab } from "./data/Datasets";
import { PipelineEditor, PipelinesTab } from "./data/Pipelines";
import { TraceDetail, TracesTab } from "./data/Traces";

type Tab = "traces" | "datasets" | "pipelines";
const TABS: Tab[] = ["traces", "datasets", "pipelines"];

/**
 * 数据中心 — Agent trajectories (observed traces), evaluation datasets and
 * data-processing pipelines. Sub-pages are `?view=` states of this route
 * (trace / dataset / dataset-new / dataset-edit / pipeline / pipeline-new).
 */
export function V2DataCenter() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab: Tab = (TABS as string[]).includes(params.get("tab") ?? "") ? (params.get("tab") as Tab) : "traces";
  const view = params.get("view");
  const id = params.get("id");

  if (view === "trace" && id) return <TraceDetail key={id} traceId={id} />;
  if (view === "dataset" && id) return <DatasetDetail key={id} id={id} />;
  if (view === "dataset-new") return <DatasetEditor id={null} />;
  if (view === "dataset-edit" && id) return <DatasetEditor key={id} id={id} />;
  if (view === "pipeline-new") return <PipelineEditor id={null} />;
  if (view === "pipeline" && id) return <PipelineEditor key={id} id={id} />;

  return (
    <>
      <PageHeader
        title={t("v2.data.title")}
        tabs={
          <SubTabs
            value={tab}
            onChange={(next) => setParams({ tab: next })}
            tabs={TABS.map((value) => ({ value, label: t(`v2.data.tab.${value}`) }))}
          />
        }
      />
      {tab === "traces" && <TracesTab />}
      {tab === "datasets" && <DatasetsTab />}
      {tab === "pipelines" && <PipelinesTab />}
    </>
  );
}

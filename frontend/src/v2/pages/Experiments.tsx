import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api } from "../../lib/api";
import { RuntimeCanaryView } from "../../pages/EvaluationRuntimeCanary";
import { useLoad } from "../hooks";
import { PageHeader, SubTabs } from "../ui";
import { ExperimentDetail } from "./experiments/ExperimentDetail";
import { ExperimentList } from "./experiments/ExperimentList";
import { ExperimentStart } from "./experiments/ExperimentStart";

type Mode = "configuration" | "canary";
const LIST_POLL_MS = 8000;

/**
 * 实验 — configuration-bundle experiments (recommend → bundles → gateway/A-B →
 * traffic → verdict → promote → cleanup) and, as a second tab, runtime canaries.
 * Sub-pages are `?view=` states: `new` (`agent=`, `lookback=`, `baselineRun=`,
 * `sourceRun=`) and `detail&id=`. `mode=canary` opens the canary tab (its own
 * `canary=` / `champion=` / `sourceExp=` params, shared with the classic page).
 */
export function V2Experiments() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const mode: Mode = params.get("mode") === "canary" ? "canary" : "configuration";
  const view = params.get("view");
  const id = params.get("id");
  const list = useLoad(() => api.v2Experiments(), "v2-experiments");
  const { reload } = list;
  useEffect(() => {
    const timer = window.setInterval(reload, LIST_POLL_MS);
    return () => window.clearInterval(timer);
  }, [reload]);
  const experiments = list.data?.experiments ?? [];
  const hasRunning = experiments.some((e) => e.status === "running");

  if (mode === "configuration" && view === "new") return <ExperimentStart hasRunning={hasRunning} />;
  if (mode === "configuration" && view === "detail" && id) return <ExperimentDetail key={id} id={id} hasRunning={hasRunning} />;

  return (
    <>
      <PageHeader
        title={t("v2.experiments.title")}
        desc={t("v2.experiments.desc")}
        tabs={
          <SubTabs
            value={mode}
            onChange={(next) => setParams(next === "canary" ? { mode: "canary" } : {})}
            tabs={(["configuration", "canary"] as Mode[]).map((value) => ({ value, label: t(`v2.experiments.mode.${value}`) }))}
          />
        }
      />
      {mode === "canary" ? (
        // runtime canaries keep the classic view (on the V2 theme) for now
        <div className="v2-classic">
          <RuntimeCanaryView />
        </div>
      ) : (
        <ExperimentList experiments={experiments} loading={list.loading} error={list.error} reload={reload} />
      )}
    </>
  );
}

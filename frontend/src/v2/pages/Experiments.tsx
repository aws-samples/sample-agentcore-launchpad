import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api } from "../../lib/api";
import { useLoad } from "../hooks";
import { PageHeader, SubTabs } from "../ui";
import { CanaryDetail } from "./canary/CanaryDetail";
import { CanaryList } from "./canary/CanaryList";
import { CanaryStart } from "./canary/CanaryStart";
import { ExperimentDetail } from "./experiments/ExperimentDetail";
import { ExperimentList } from "./experiments/ExperimentList";
import { ExperimentStart } from "./experiments/ExperimentStart";

type Mode = "configuration" | "canary";
const LIST_POLL_MS = 8000;

/**
 * 实验 — runtime canaries (the first, default tab) and configuration-bundle
 * experiments (recommend → bundles → gateway/A-B → traffic → verdict → promote →
 * cleanup). `mode=configuration` opens the experiments tab; its sub-pages are
 * `?view=` states — `new` (`agent=`, `lookback=`, `baselineRun=`, `sourceRun=`)
 * and `detail&id=` — which open without `mode` (they predate the canary default).
 * Canary params are the classic page's: `canary=new` (with the promote hand-off's
 * `champion=` / `sourceExp=`) or `canary=<id>`, alongside `mode=canary`.
 */
export function V2Experiments() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const view = params.get("view");
  const modeParam = params.get("mode");
  const mode: Mode =
    modeParam === "configuration" || (modeParam !== "canary" && (view === "new" || view === "detail")) ? "configuration" : "canary";
  const id = params.get("id");
  const canaryParam = mode === "canary" ? params.get("canary") : null;
  const list = useLoad(() => api.v2Experiments(), "v2-experiments");
  // off the canary tab nothing is fetched; null (not an empty list) so the tab's
  // attention panel waits for the first real read instead of saying "nothing"
  const canaries = useLoad(
    () => (mode === "canary" ? api.listRuntimeCanaries() : Promise.resolve(null)),
    `v2-canaries:${mode}`,
  );
  const { reload } = list;
  const reloadCanaries = canaries.reload;
  useEffect(() => {
    const timer = window.setInterval(() => {
      reload();
      if (mode === "canary") reloadCanaries();
    }, LIST_POLL_MS);
    return () => window.clearInterval(timer);
  }, [reload, reloadCanaries, mode]);
  const experiments = list.data?.experiments ?? [];
  const hasRunning = experiments.some((e) => e.status === "running");

  if (canaryParam === "new") return <CanaryStart />;
  if (canaryParam) return <CanaryDetail key={canaryParam} id={canaryParam} />;
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
            onChange={(next) => setParams({ mode: next })}
            tabs={(["canary", "configuration"] as Mode[]).map((value) => ({ value, label: t(`v2.experiments.mode.${value}`) }))}
          />
        }
      />
      {mode === "canary" ? (
        <CanaryList
          canaries={canaries.data?.canaries ?? []}
          loaded={canaries.data !== null}
          loading={canaries.loading}
          error={canaries.error}
          reload={reloadCanaries}
        />
      ) : (
        <ExperimentList
          experiments={experiments}
          loaded={list.data !== null}
          loading={list.loading}
          error={list.error}
          reload={reload}
        />
      )}
    </>
  );
}

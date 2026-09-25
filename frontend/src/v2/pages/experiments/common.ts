import type { TFunction } from "i18next";

import type { ExperimentInfo } from "../../../lib/experiments";
import type { TagTone } from "../../ui";

export const EXPERIMENT_TONE: Record<string, TagTone> = {
  running: "blue",
  ready: "green",
  promoted: "green",
  failed: "red",
  cleaned: "gray",
};

/** The stage the experiment is at — or the action running right now. */
export function stageLabel(t: TFunction, exp: Pick<ExperimentInfo, "stage" | "running_action">): string {
  const key = exp.running_action ?? exp.stage;
  const name = t(`v2.experiments.stage.${key}`, { defaultValue: key });
  return exp.running_action ? t("v2.experiments.stageRunning", { stage: name }) : name;
}

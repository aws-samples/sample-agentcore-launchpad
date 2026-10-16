// Copy helpers of the agent scorecard views: how a benchmark, a run's scope and a run's
// version read. Kept apart from scorecardUi.tsx so that file exports components only.
import type { TFunction } from "i18next";

import type { RunBenchmark, RunSnapshotRef, ScorecardRun } from "../../../lib/api";
import type { EvaluationRunStatus } from "../../../lib/evaluation";
import { evaluatorLabel } from "../../../lib/evaluators";
import { type RerunBlock, windowHours } from "../../scorecard";
import type { TaskStatus } from "../../tasks";

/** A benchmark's name: the dataset's, or "live traffic". */
export function benchName(t: TFunction, bench: Pick<RunBenchmark, "kind" | "label"> & { dataset_id?: string | null }): string {
  return bench.kind === "live" ? t("v2.compare.live") : bench.label || bench.dataset_id || "—";
}

/** A run's name; its id when unnamed. */
export function runName(run: Pick<ScorecardRun, "id" | "name">): string {
  return run.name || run.id;
}

/** The compact version text of a table sub-line: "#3", "≈ #3" when inferred or observed,
 *  "" without a version. */
export function versionShort(t: TFunction, snapshot: RunSnapshotRef | null | undefined): string {
  return snapshot ? t(`v2.scorecard.versionShort.${snapshot.source}`, { seq: snapshot.seq }) : "";
}

/** The version's hover text (the compare page's hints). */
export function versionHint(t: TFunction, snapshot: RunSnapshotRef | null | undefined): string {
  return snapshot ? t(`v2.compare.version.${snapshot.source}Hint`, { seq: snapshot.seq }) : t("v2.compare.version.noneHint");
}

/** What a run read, as the task list says it ("数据集 · hr-qa", "链路 · 近 24h", …), with
 *  a cloud run's pinned version. */
export function runSourceText(t: TFunction, run: ScorecardRun): string {
  switch (run.scope) {
    case "dataset":
      return t("v2.taskSource.dataset", { detail: benchName(t, run.benchmark) });
    case "cloud": {
      const text = t("v2.taskSource.cloud", { detail: benchName(t, run.benchmark) });
      return run.dataset_version ? `${text} · v${run.dataset_version}` : text;
    }
    case "window": {
      const hours = windowHours(run.dataset_name);
      return t("v2.taskSource.window", { detail: hours != null ? `${hours}h` : "—" });
    }
    case "logs":
      return t("v2.taskSource.logs", { detail: String(run.sessions) });
    default:
      return t("v2.taskSource.sessions", { detail: String(run.sessions) });
  }
}

/** Evaluator names joined ("正确性、有用性"). */
export function evaluatorList(t: TFunction, ids: string[]): string {
  return ids.map((id) => evaluatorLabel(t, id)).join(t("v2.compare.sep"));
}

/** A ledger run status as the task list shows it: replaying, waiting and judging all
 *  read as running (`v2.taskStatus.*`, `STATUS_TONE`). */
export function taskStatusOf(status: EvaluationRunStatus): TaskStatus {
  return status === "invoking" || status === "waiting" || status === "evaluating" ? "running" : status;
}

/** Why 再评一次 is disabled (its button tooltip); undefined when it is enabled. */
export function rerunBlockText(t: TFunction, block: RerunBlock | null, mayRun: boolean): string | undefined {
  if (block) return t(`v2.scorecard.rerun.block.${block}`);
  return mayRun ? undefined : t("v2.tasks.noPermission");
}

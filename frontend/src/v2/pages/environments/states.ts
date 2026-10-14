// Pure logic of the Environments page (`/v2/environments`): compare states → a Signal
// and a label, and the compare summary. Mirrors `backend/app/services/environments.py`
// so vitest covers it without a DOM.
import type { EnvironmentCompareState, EnvironmentComparison } from "../../../lib/api";
import type { Signal } from "../../signals";
import { groupLabelKey } from "../agents/snapshots";

export const COMPARE_SIGNAL: Record<EnvironmentCompareState, Signal> = {
  baseline: "info",
  same: "ok",
  differs: "wait",
  absent: "off",
  no_baseline: "off",
};

export function compareStateKey(state: EnvironmentCompareState): string {
  return `v2.environments.compareState.${state}`;
}

/** The label keys of a compare row's differing groups (spec groups + "image"). */
export function changedGroupKeys(groups: string[]): string[] {
  return groups.map(groupLabelKey);
}

export interface CompareSummary {
  tone: "success" | "warn" | "info";
  key: string;
  params: Record<string, unknown>;
}

/** The one-line verdict above the compare table. */
export function compareSummary(result: EnvironmentComparison): CompareSummary {
  const params = { agent: result.agent, differs: result.summary.differs };
  if (result.summary.present === 0) return { tone: "info", key: "v2.environments.nowhere", params };
  const baseline = result.environments.find((row) => row.baseline);
  if (!baseline?.present) return { tone: "warn", key: "v2.environments.noBaseline", params };
  if (result.summary.aligned) return { tone: "success", key: "v2.environments.aligned", params };
  return { tone: "warn", key: "v2.environments.notAligned", params };
}

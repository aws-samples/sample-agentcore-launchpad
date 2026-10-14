// Pure logic of the Environments page (`/v2/environments`): compare / drift states →
// a Signal and a label, the drift detail line, the drift table order and the compare
// summary. Mirrors `backend/app/services/environments.py` so vitest covers it without
// a DOM. "Unknown" is its own colour and never reads as in sync: a green lamp must mean
// that AWS was read and agreed.
import type {
  DriftAgent,
  DriftState,
  EnvironmentCompareState,
  EnvironmentComparison,
} from "../../../lib/api";
import type { Signal } from "../../signals";
import { groupLabelKey } from "../agents/snapshots";

export const COMPARE_SIGNAL: Record<EnvironmentCompareState, Signal> = {
  baseline: "info",
  same: "ok",
  differs: "wait",
  absent: "off",
  no_baseline: "off",
};

export const DRIFT_SIGNAL: Record<DriftState, Signal> = {
  in_sync: "ok",
  drift: "act",
  unknown: "wait",
};

export function compareStateKey(state: EnvironmentCompareState): string {
  return `v2.environments.compareState.${state}`;
}

export function driftStateKey(state: DriftState): string {
  return `v2.environments.state.${state}`;
}

/** The label keys of a compare row's differing groups (spec groups + "image"). */
export function changedGroupKeys(groups: string[]): string[] {
  return groups.map(groupLabelKey);
}

export interface Phrase {
  key: string;
  params?: Record<string, unknown>;
}

/** What a drift row says: every finding, else why it is unknown, else nothing. */
export function driftDetail(agent: Pick<DriftAgent, "findings" | "reason">): Phrase[] {
  if (agent.findings.length) {
    return agent.findings.map((f) => ({
      key: `v2.environments.finding.${f.code}`,
      params: { expected: f.expected ?? "—", observed: f.observed ?? "—" },
    }));
  }
  return agent.reason ? [{ key: `v2.environments.reason.${agent.reason}` }] : [];
}

const DRIFT_ORDER: DriftState[] = ["drift", "unknown", "in_sync"];

/** Drifted first, then unknown, then in sync; the server's (name) order otherwise. */
export function rankDrift<T extends Pick<DriftAgent, "state">>(agents: T[]): T[] {
  return [...agents].sort((a, b) => DRIFT_ORDER.indexOf(a.state) - DRIFT_ORDER.indexOf(b.state));
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

// The configuration experiments and runtime canaries that are moving or waiting on a
// decision (ported from the V3 console). Pure: each list turns the entries into rows and
// owns the copy and the navigation.
import type { RuntimeCanaryInfo } from "../../../lib/api";
import type { ExperimentInfo } from "../../../lib/experiments";
import { rankBySignal, type Signal } from "../../signals";

/** `moving`: a stage runs on its own (info). `decide`: a verdict waits on a person (wait). */
export type FlightKind = "moving" | "decide";

export interface ExperimentEntry {
  key: string;
  kind: FlightKind;
  signal: Signal;
  experiment: ExperimentInfo;
}

export interface CanaryEntry {
  key: string;
  kind: FlightKind;
  signal: Signal;
  canary: RuntimeCanaryInfo;
}

/** An action in flight is moving whatever the status; a `ready` experiment has its verdict
 *  and waits on promote / clean up. Promoted, failed and cleaned runs are not queued: a
 *  failed run needs cleanup but nobody waits on it (the list shows it red). */
export function experimentKind(e: Pick<ExperimentInfo, "status" | "running_action">): FlightKind | null {
  if (e.running_action || e.status === "running") return "moving";
  if (e.status === "ready") return "decide";
  return null;
}

/** An action in flight is moving; a running canary at the verdict stage waits on a call
 *  (ramp on, complete or roll back); any other running stage is moving. */
export function canaryKind(c: Pick<RuntimeCanaryInfo, "status" | "stage" | "running_action">): FlightKind | null {
  if (c.running_action) return "moving";
  if (c.status !== "running") return null;
  return c.stage === "verdict" ? "decide" : "moving";
}

const signalOf = (kind: FlightKind): Signal => (kind === "decide" ? "wait" : "info");

/** Waiting on a decision before moving on its own; list order otherwise. */
export function experimentQueue(experiments: ExperimentInfo[]): ExperimentEntry[] {
  const out: ExperimentEntry[] = [];
  for (const experiment of experiments) {
    const kind = experimentKind(experiment);
    if (kind) out.push({ key: `${experiment.id}:${kind}`, kind, signal: signalOf(kind), experiment });
  }
  return rankBySignal(out);
}

export function canaryQueue(canaries: RuntimeCanaryInfo[]): CanaryEntry[] {
  const out: CanaryEntry[] = [];
  for (const canary of canaries) {
    const kind = canaryKind(canary);
    if (kind) out.push({ key: `${canary.id}:${kind}`, kind, signal: signalOf(kind), canary });
  }
  return rankBySignal(out);
}

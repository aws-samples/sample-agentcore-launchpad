// Data center helpers: one signal per status, the V2 range vocabulary, and the
// dataset shape helpers the list and the detail share.
import type { V2Dataset, V2Pipeline, V2Range } from "../../../lib/api";
import { scenarioKind } from "../../../lib/datasetDrafts";
import { RANGES } from "../../../v2/format";
import type { Signal } from "../../ui";

export function asRange(value: string | null): V2Range {
  return (RANGES as string[]).includes(value ?? "") ? (value as V2Range) : "24h";
}

/** "trace" when the items came from observed trajectories, else "manual" (as V2). */
export function datasetOrigin(ds: V2Dataset): "trace" | "manual" {
  return ds.items.some((i) => (i.metadata as { source?: string } | undefined)?.source === "trace") ? "trace" : "manual";
}

/** A dataset's cloud copy: synced is in service, a modified draft waits on a sync. */
export function cloudSignal(ds: V2Dataset): Signal {
  if (!ds.cloud?.dataset_id) return "off";
  return ds.cloud.draft_status === "MODIFIED" ? "wait" : "ok";
}

export function pipelineSignal(status: V2Pipeline["status"]): Signal {
  if (status === "succeeded") return "ok";
  if (status === "running") return "wait";
  if (status === "failed") return "act";
  return "off";
}

export interface DatasetRow {
  input: string;
  expected: string;
  /** turns beyond the first */
  extraTurns: number;
  original: Record<string, unknown> | null;
}

/** The first-turn view of each item, exactly as V2's dataset detail reads it. */
export function datasetRows(ds: V2Dataset): DatasetRow[] {
  return ds.items.map((item) => {
    if (Array.isArray(item.turns)) {
      const turns = item.turns as { input?: unknown; expected_response?: unknown }[];
      const first = turns[0] ?? {};
      return {
        input: String(first.input ?? ""),
        expected: String(first.expected_response ?? ""),
        extraTurns: Math.max(0, turns.length - 1),
        original: item,
      };
    }
    if (scenarioKind(item) === "dynamic") return { input: String(item.input ?? ""), expected: "", extraTurns: 0, original: item };
    return { input: String(item.prompt ?? ""), expected: String(item.expected ?? ""), extraTurns: 0, original: item };
  });
}

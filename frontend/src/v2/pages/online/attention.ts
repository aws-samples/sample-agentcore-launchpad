// The online evaluation configs that need someone (ported from the V3 console). Pure:
// the config list turns each entry into a row and owns the copy and the navigation.
import type { OnlineEvalConfigRow } from "../../../lib/api";
import { isTransient } from "../../online";
import type { Signal } from "../../signals";

export type OnlineAttentionKind = "failed" | "duplicate" | "settling";

export interface OnlineEntry {
  key: string;
  kind: OnlineAttentionKind;
  signal: Signal;
  configId: string;
  row: OnlineEvalConfigRow;
}

const FAILED = new Set(["CREATE_FAILED", "UPDATE_FAILED", "ERROR"]);

/** Queue order: a failure first, then a config judging twice, then one still settling. */
const ORDER: OnlineAttentionKind[] = ["failed", "duplicate", "settling"];

/** Why a config needs a look, or null. One reason per config, most serious first: a
 *  paused duplicate judges nothing, so it is not queued. */
export function onlineAttentionKind(row: OnlineEvalConfigRow): OnlineAttentionKind | null {
  if (FAILED.has(row.status ?? "")) return "failed";
  if (row.duplicate_enabled && row.execution_status === "ENABLED") return "duplicate";
  if (isTransient(row)) return "settling";
  return null;
}

export function onlineAttention(configs: OnlineEvalConfigRow[]): OnlineEntry[] {
  const out: OnlineEntry[] = [];
  for (const row of configs) {
    const kind = onlineAttentionKind(row);
    if (!kind) continue;
    out.push({ key: `${row.config_id}:${kind}`, kind, signal: kind === "failed" ? "act" : "wait", configId: row.config_id, row });
  }
  // stable: within one kind the list order is kept
  return out.sort((a, b) => ORDER.indexOf(a.kind) - ORDER.indexOf(b.kind));
}

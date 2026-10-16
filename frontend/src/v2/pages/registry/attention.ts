// The records waiting on an approval decision (ported from the V3 console). Pure: the
// record list turns each entry into a row and owns the copy, navigation and the inline
// approve / reject calls.
import type { RegistryRecordOut } from "../../../lib/api";
import { rankBySignal, type Signal } from "../../signals";

export type RegistryAttentionKind = "pending";

export interface RegistryEntry {
  key: string;
  kind: RegistryAttentionKind;
  signal: Signal;
  recordId: string;
  /** values the copy interpolates; `version` is "" when the record has none */
  values: { name: string; type: RegistryRecordOut["type"]; version: string; detail: string };
}

/** Rows the panel shows (`AttentionPanel` `cap`); the header counts them all. */
export const APPROVAL_CAP = 8;

/** Longest hint a row shows before it is cut. */
export const HINT_MAX = 90;

const clip = (text: string) => (text.length > HINT_MAX ? `${text.slice(0, HINT_MAX - 1)}…` : text);

/** `PENDING_APPROVAL` records the viewer may move; the panel caps them at `APPROVAL_CAP`.
 *  A system-managed record's lifecycle is an administrator's call: the server refuses a
 *  member, so the queue hides it from one. Every entry is `wait`, so the API order is kept
 *  (no sort by date). */
export function approvalQueue(records: RegistryRecordOut[], isAdmin: boolean): RegistryEntry[] {
  const out: RegistryEntry[] = records
    .filter((r) => r.status === "PENDING_APPROVAL" && (!r.system || isAdmin))
    .map((r) => ({
      key: `${r.record_id}:pending`,
      kind: "pending",
      signal: "wait",
      recordId: r.record_id,
      values: {
        name: r.name,
        type: r.type,
        version: r.version ?? "",
        detail: clip(r.description || r.record_id),
      },
    }));
  return rankBySignal(out);
}

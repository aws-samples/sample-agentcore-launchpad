// Pure logic of the agent detail's Snapshots card: change groups and their labels, how
// one field-level change reads (scalar before/after, list + / −, an elided long body),
// which rows offer Roll back, the two-row compare pick, and the server's notes. Mirrors
// `backend/app/services/snapshots.py` (GROUP_ORDER, _ATOMIC, BODY_LIMIT) so vitest
// covers it without a DOM.
import type { SnapshotState, SpecChange, SpecChangeGroup, SpecSnapshotInfo } from "../../../lib/api";
import type { TagTone } from "../../ui";

/** Display order of the change groups (the backend's GROUP_ORDER, then "other"). */
export const SNAPSHOT_GROUPS: SpecChangeGroup[] = [
  "prompt",
  "model",
  "tools",
  "skills",
  "knowledge_bases",
  "memory",
  "guardrail",
  "other",
];

/** The i18n key of a change group; anything unknown reads as "other". */
export function groupLabelKey(group: string): string {
  const known = (SNAPSHOT_GROUPS as string[]).includes(group);
  return `v2.agents.snapshots.group.${known ? group : "other"}`;
}

/** Changes bucketed by group, in display order; a group keeps the server's order. */
export function groupChanges(changes: SpecChange[]): { group: SpecChangeGroup; changes: SpecChange[] }[] {
  const by = new Map<SpecChangeGroup, SpecChange[]>();
  for (const change of changes) {
    const group = SNAPSHOT_GROUPS.includes(change.group) ? change.group : "other";
    const bucket = by.get(group);
    if (bucket) bucket.push(change);
    else by.set(group, [change]);
  }
  return SNAPSHOT_GROUPS.filter((g) => by.has(g)).map((group) => ({ group, changes: by.get(group) ?? [] }));
}

export const SNAPSHOT_TONE: Record<SnapshotState, TagTone> = {
  deployed: "green",
  deploying: "blue",
  failed: "red",
  not_deployed: "gray",
};

export function snapshotStateKey(state: SnapshotState): string {
  return `v2.agents.snapshots.state.${state}`;
}

/** A before / after value as text: strings verbatim, structures as indented JSON. */
export function valueText(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return value === "" ? '""' : value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value, null, 2);
}

/** One list member on a single line: strings verbatim, structures as compact JSON. */
export function itemText(item: unknown): string {
  if (item === null || item === undefined) return "—";
  if (typeof item === "string") return item;
  if (typeof item === "object") return JSON.stringify(item);
  return String(item);
}

export type SideView =
  /** the field is not set on this side (an `added` change's before, a `removed` one's after) */
  | { kind: "absent" }
  /** a whole-compared body over the backend's 2 KB limit: only its length came back */
  | { kind: "elided"; length: number }
  | { kind: "text"; text: string };

export type ChangeView =
  | { kind: "list"; removed: string[]; added: string[] }
  | { kind: "values"; before: SideView; after: SideView };

function isLengthStub(value: unknown): value is { length: number } {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.keys(value).length === 1 &&
    typeof (value as { length?: unknown }).length === "number"
  );
}

function side(change: SpecChange, which: "before" | "after"): SideView {
  const absent = which === "before" ? change.kind === "added" : change.kind === "removed";
  if (absent) return { kind: "absent" };
  const value = change[which];
  if (change.elided && isLengthStub(value)) return { kind: "elided", length: value.length };
  return { kind: "text", text: valueText(value) };
}

/**
 * How one change reads. A list field shows its members gained (+) and lost (−) — also
 * when the field was absent or null on one side, so a newly added tool list still reads
 * as "+ tool". A pure re-order (nothing gained or lost) falls back to before / after.
 */
export function changeView(change: SpecChange): ChangeView {
  if (Array.isArray(change.added) && Array.isArray(change.removed)) {
    if (change.added.length || change.removed.length) {
      return { kind: "list", removed: change.removed.map(itemText), added: change.added.map(itemText) };
    }
  } else {
    const before = change.kind === "added" ? null : change.before;
    const after = change.kind === "removed" ? null : change.after;
    const lost = before == null ? [] : before;
    const gained = after == null ? [] : after;
    if (
      Array.isArray(lost) &&
      Array.isArray(gained) &&
      (Array.isArray(before) || Array.isArray(after)) &&
      (lost.length || gained.length)
    ) {
      return { kind: "list", removed: lost.map(itemText), added: gained.map(itemText) };
    }
  }
  return { kind: "values", before: side(change, "before"), after: side(change, "after") };
}

/** Whole-compared string fields whose full text the card can fetch when elided. */
const FULL_TEXT_FIELDS = new Set(["system_prompt", "code"]);

export function canShowFull(change: SpecChange): boolean {
  return change.elided === true && FULL_TEXT_FIELDS.has(change.path);
}

/** The value at a dotted spec path; undefined when any step is missing. */
export function valueAtPath(spec: Record<string, unknown> | null | undefined, path: string): unknown {
  let node: unknown = spec;
  for (const key of path.split(".")) {
    if (node === null || typeof node !== "object" || Array.isArray(node)) return undefined;
    node = (node as Record<string, unknown>)[key];
  }
  return node;
}

/** The newest snapshot's seq (the configuration the agent holds now); null when none. */
export function newestSeq(rows: Pick<SpecSnapshotInfo, "seq">[]): number | null {
  return rows.length ? Math.max(...rows.map((r) => r.seq)) : null;
}

/** A row offers Roll back when it deployed successfully and is not the newest (the
 *  newest is what the agent already holds — or, after a failed publish, what failed). */
export function rollbackable(row: Pick<SpecSnapshotInfo, "seq" | "deployed">, newest: number | null): boolean {
  return row.deployed && row.seq !== newest;
}

/** Tick / untick a row; at most two stay picked (a third pick drops the oldest pick). */
export function togglePick(picked: number[], seq: number): number[] {
  return picked.includes(seq) ? picked.filter((s) => s !== seq) : [...picked, seq].slice(-2);
}

/** The `[older, newer]` pair to diff when exactly two rows are picked; null otherwise. */
export function pickCompare(picked: number[]): [number, number] | null {
  if (picked.length !== 2) return null;
  const [a, b] = picked;
  return a < b ? [a, b] : [b, a];
}

export type NoteLabel = { key: string; params?: Record<string, unknown> } | { text: string };

/** The server writes a handful of fixed notes; those get copy, anything else verbatim. */
export function noteLabel(note: string | null | undefined): NoteLabel | null {
  if (!note) return null;
  const rollback = /^rollback to #(\d+)$/.exec(note);
  if (rollback) return { key: "v2.agents.snapshots.noteKind.rollback", params: { seq: Number(rollback[1]) } };
  const fixed: Record<string, string> = {
    baseline: "baseline",
    "canary promoted": "canaryPromoted",
    "canary rolled back": "canaryRolledBack",
    "inbound auth switched": "inboundAuth",
    "recommendation accepted": "recommendation",
  };
  return fixed[note] ? { key: `v2.agents.snapshots.noteKind.${fixed[note]}` } : { text: note };
}

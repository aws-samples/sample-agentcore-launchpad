import { describe, expect, it } from "vitest";

import type { SpecChange } from "../../../lib/api";
import {
  canShowFull,
  changeView,
  groupChanges,
  groupLabelKey,
  itemText,
  newestSeq,
  noteLabel,
  pickCompare,
  SNAPSHOT_GROUPS,
  SNAPSHOT_TONE,
  snapshotStateKey,
  togglePick,
  valueAtPath,
  valueText,
} from "./snapshots";

function change(over: Partial<SpecChange>): SpecChange {
  return { path: "model_id", group: "model", kind: "changed", before: null, after: null, ...over };
}

describe("groups", () => {
  it("mirrors the backend's GROUP_ORDER, then other", () => {
    expect(SNAPSHOT_GROUPS).toEqual([
      "prompt",
      "model",
      "tools",
      "skills",
      "knowledge_bases",
      "memory",
      "guardrail",
      "other",
    ]);
  });

  it("labels every group and folds unknowns into other", () => {
    expect(groupLabelKey("knowledge_bases")).toBe("v2.agents.snapshots.group.knowledge_bases");
    expect(groupLabelKey("image")).toBe("v2.agents.snapshots.group.other");
    expect(groupLabelKey("mystery")).toBe("v2.agents.snapshots.group.other");
  });

  it("buckets changes in display order and keeps the order inside a group", () => {
    const grouped = groupChanges([
      change({ path: "guardrail.id", group: "guardrail" }),
      change({ path: "tools", group: "tools" }),
      change({ path: "system_prompt", group: "prompt" }),
      change({ path: "native_tools", group: "tools" }),
      change({ path: "weird", group: "nope" as SpecChange["group"] }),
    ]);
    expect(grouped.map((g) => g.group)).toEqual(["prompt", "tools", "guardrail", "other"]);
    expect(grouped[1].changes.map((c) => c.path)).toEqual(["tools", "native_tools"]);
    expect(grouped[3].changes.map((c) => c.path)).toEqual(["weird"]);
  });

  it("an empty diff has no groups", () => {
    expect(groupChanges([])).toEqual([]);
  });
});

describe("value formatting", () => {
  it("shows scalars plainly and structures as indented JSON", () => {
    expect(valueText("claude")).toBe("claude");
    expect(valueText("")).toBe('""');
    expect(valueText(4096)).toBe("4096");
    expect(valueText(false)).toBe("false");
    expect(valueText(null)).toBe("—");
    expect(valueText(undefined)).toBe("—");
    expect(valueText({ a: 1 })).toBe('{\n  "a": 1\n}');
  });

  it("shows list members on one line", () => {
    expect(itemText("web_search")).toBe("web_search");
    expect(itemText({ kb_id: "KB1", name: "hr" })).toBe('{"kb_id":"KB1","name":"hr"}');
    expect(itemText(3)).toBe("3");
    expect(itemText(null)).toBe("—");
  });
});

describe("changeView", () => {
  it("a scalar change reads before / after", () => {
    expect(changeView(change({ before: "a", after: "b" }))).toEqual({
      kind: "values",
      before: { kind: "text", text: "a" },
      after: { kind: "text", text: "b" },
    });
  });

  it("an added or removed scalar has an absent side", () => {
    expect(changeView(change({ kind: "added", before: null, after: 0.2 }))).toEqual({
      kind: "values",
      before: { kind: "absent" },
      after: { kind: "text", text: "0.2" },
    });
    expect(changeView(change({ kind: "removed", before: "x", after: null }))).toEqual({
      kind: "values",
      before: { kind: "text", text: "x" },
      after: { kind: "absent" },
    });
  });

  it("a list change reads its members gained and lost", () => {
    const view = changeView(
      change({ path: "tools", group: "tools", before: ["a", "b"], after: ["b", "c"], added: ["c"], removed: ["a"] }),
    );
    expect(view).toEqual({ kind: "list", removed: ["a"], added: ["c"] });
  });

  it("a pure re-order falls back to before / after", () => {
    const view = changeView(
      change({ path: "tools", group: "tools", before: ["a", "b"], after: ["b", "a"], added: [], removed: [] }),
    );
    expect(view.kind).toBe("values");
  });

  it("a list that appears or disappears reads as all + or all −", () => {
    expect(changeView(change({ path: "skills", group: "skills", kind: "added", after: ["s3://x"] }))).toEqual({
      kind: "list",
      removed: [],
      added: ["s3://x"],
    });
    expect(
      changeView(change({ path: "knowledge_bases", group: "knowledge_bases", before: [{ kb_id: "K" }], after: null })),
    ).toEqual({ kind: "list", removed: ['{"kb_id":"K"}'], added: [] });
  });

  it("an empty list appearing shows as a value, not an empty list", () => {
    expect(changeView(change({ path: "tools", group: "tools", kind: "added", after: [] })).kind).toBe("values");
  });

  it("an elided side reads its length; the other side stays text", () => {
    const view = changeView(
      change({ path: "system_prompt", group: "prompt", before: { length: 5000 }, after: "short", elided: true }),
    );
    expect(view).toEqual({
      kind: "values",
      before: { kind: "elided", length: 5000 },
      after: { kind: "text", text: "short" },
    });
  });

  it("a {length} object is not elided unless the server said so", () => {
    const view = changeView(change({ path: "studio_flow", group: "other", before: { length: 3 }, after: null }));
    expect(view.kind === "values" && view.before.kind).toBe("text");
  });

  it("offers the full text only for elided prompt / code", () => {
    expect(canShowFull(change({ path: "system_prompt", elided: true }))).toBe(true);
    expect(canShowFull(change({ path: "code", elided: true }))).toBe(true);
    expect(canShowFull(change({ path: "code_bundle", elided: true }))).toBe(false);
    expect(canShowFull(change({ path: "system_prompt" }))).toBe(false);
  });

  it("reads a dotted path out of a spec", () => {
    const spec = { system_prompt: "hi", memory: { long_term: { enabled: true } } };
    expect(valueAtPath(spec, "system_prompt")).toBe("hi");
    expect(valueAtPath(spec, "memory.long_term.enabled")).toBe(true);
    expect(valueAtPath(spec, "memory.short_term.x")).toBeUndefined();
    expect(valueAtPath(null, "a")).toBeUndefined();
  });
});

describe("rows", () => {
  const rows = [
    { seq: 3, deployed: false },
    { seq: 2, deployed: true },
    { seq: 1, deployed: true },
  ];

  it("the newest is the highest seq", () => {
    expect(newestSeq(rows)).toBe(3);
    expect(newestSeq([])).toBeNull();
  });

  it("maps every state to a tone and a label", () => {
    expect(SNAPSHOT_TONE).toEqual({ deployed: "green", deploying: "blue", failed: "red", not_deployed: "gray" });
    expect(snapshotStateKey("not_deployed")).toBe("v2.agents.snapshots.state.not_deployed");
  });
});

describe("compare pick", () => {
  it("keeps at most two picks, dropping the oldest pick", () => {
    expect(togglePick([], 3)).toEqual([3]);
    expect(togglePick([3], 1)).toEqual([3, 1]);
    expect(togglePick([3, 1], 2)).toEqual([1, 2]);
    expect(togglePick([1, 2], 1)).toEqual([2]);
  });

  it("diffs older → newer, whatever order they were picked in", () => {
    expect(pickCompare([5, 2])).toEqual([2, 5]);
    expect(pickCompare([2, 5])).toEqual([2, 5]);
    expect(pickCompare([2])).toBeNull();
    expect(pickCompare([])).toBeNull();
  });
});

describe("noteLabel", () => {
  it("translates the server's fixed notes", () => {
    expect(noteLabel("baseline")).toEqual({ key: "v2.agents.snapshots.noteKind.baseline" });
    expect(noteLabel("rollback to #4")).toEqual({ key: "v2.agents.snapshots.noteKind.rollback", params: { seq: 4 } });
    expect(noteLabel("canary promoted")).toEqual({ key: "v2.agents.snapshots.noteKind.canaryPromoted" });
    expect(noteLabel("canary rolled back")).toEqual({ key: "v2.agents.snapshots.noteKind.canaryRolledBack" });
    expect(noteLabel("inbound auth switched")).toEqual({ key: "v2.agents.snapshots.noteKind.inboundAuth" });
    expect(noteLabel("recommendation accepted")).toEqual({ key: "v2.agents.snapshots.noteKind.recommendation" });
  });

  it("shows any other note verbatim, and nothing for none", () => {
    expect(noteLabel("hand-written")).toEqual({ text: "hand-written" });
    expect(noteLabel(null)).toBeNull();
    expect(noteLabel("")).toBeNull();
  });
});

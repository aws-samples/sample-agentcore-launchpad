import { describe, expect, it } from "vitest";

import { applyToolDetail, type ChatMessage } from "./messages";

// N16-obs85: `tool_input` / `tool_result` frames complete the tool row the call opened —
// matched on the runtime's tool-use id, by name when neither side has an id, never invented.
const base: ChatMessage[] = [
  { kind: "user", text: "open the portal" },
  { kind: "agent", text: "Opening… " },
  { kind: "tool", text: "browser", name: "browser", toolId: "t1" },
  { kind: "tool", text: "search", name: "search", toolId: "t2" },
];

describe("applyToolDetail", () => {
  it("matches the frame's id and keeps the other rows untouched", () => {
    const out = applyToolDetail(base, { id: "t1", name: "browser", input: '{"url": "https://hr.example.test/"}' });
    expect(out[2]).toEqual({ ...base[2], toolInput: '{"url": "https://hr.example.test/"}' });
    expect(out[3]).toBe(base[3]);
    const done = applyToolDetail(out, { id: "t1", summary: "title: HR | url: https://hr.example.test/home | text: 20800 chars", status: "success" });
    expect(done[2].toolResult).toBe("title: HR | url: https://hr.example.test/home | text: 20800 chars");
    expect(done[2].toolStatus).toBe("success");
    expect(done[2].toolInput).toBe('{"url": "https://hr.example.test/"}');
  });

  it("falls back to the newest row of that name, then the newest tool row, when there is no id", () => {
    const rows: ChatMessage[] = [
      { kind: "tool", text: "browser", name: "browser" },
      { kind: "tool", text: "search", name: "search" },
      { kind: "tool", text: "browser", name: "browser" },
    ];
    const byName = applyToolDetail(rows, { name: "search", input: '{"q": "x"}' });
    expect(byName.map((r) => r.toolInput)).toEqual([undefined, '{"q": "x"}', undefined]);
    const newest = applyToolDetail(rows, { summary: "ok", status: "error" });
    expect(newest.map((r) => r.toolResult)).toEqual([undefined, undefined, "ok"]);
    expect(newest[2].toolStatus).toBe("error");
  });

  it("drops a frame nothing matches instead of inventing a row", () => {
    expect(applyToolDetail(base, { id: "t-missing", input: "{}" })).toBe(base);
    expect(applyToolDetail([{ kind: "agent", text: "no tools" }], { summary: "x" })).toHaveLength(1);
  });
});

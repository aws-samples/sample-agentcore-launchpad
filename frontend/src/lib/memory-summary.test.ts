import { describe, expect, it } from "vitest";

import type { MemoryNamespace, MemoryRecord } from "./api";
import { isFullSession, MemoryRequestScope, scopedMemoryRecord, summaryNamespaceKey, summaryNamespaces, summaryPageStatus } from "./memory-summary";

const namespace = (patch: Partial<MemoryNamespace> = {}): MemoryNamespace => ({
  strategy_id: "summary", strategy_name: "Session summary", strategy_type: "SUMMARIZATION",
  template: "/summaries/{actorId}/{sessionId}", namespace: "/summaries/a__human/session-1",
  resolvable: true, prefix: false, ...patch,
});

const record = (record_type?: string | null): MemoryRecord => ({
  record_id: "rec", record_type, text: "recap", raw_text: "recap", structured: null,
  strategy_id: "summary", namespaces: [], created_at: null, score: null, metadata: {},
});

describe("full session summary identity", () => {
  it("only offers resolvable exact summary namespaces", () => {
    const usable = namespace();
    expect(summaryNamespaces([usable, namespace({ prefix: true }), namespace({ resolvable: false }),
      namespace({ strategy_type: "SEMANTIC" }), namespace({ strategy_type: "CUSTOM" })])).toEqual([usable]);
  });

  it("keeps strategies, sessions and memory sources distinct", () => {
    const base = summaryNamespaceKey(namespace());
    expect(summaryNamespaceKey(namespace({ source: "shared" }))).not.toBe(base);
    expect(summaryNamespaceKey(namespace({ strategy_id: "other" }))).not.toBe(base);
    expect(summaryNamespaceKey(namespace({ namespace: "/summaries/a__human/session-2" }))).not.toBe(base);
  });

  it("keeps empty continuation pages pending and preserves populated summaries", () => {
    expect(summaryPageStatus([])).toBe("pending");
    expect(summaryPageStatus([...[], ...[]])).toBe("pending");
    expect(summaryPageStatus([record("FULL_SESSION")])).toBe("ready");
    expect(summaryPageStatus([record("FULL_SESSION"), ...[]])).toBe("ready");
  });

  it("does not label absent or unknown types as full summaries", () => {
    expect(isFullSession(record("FULL_SESSION"))).toBe(true);
    for (const type of [undefined, null, "FUTURE", "CHUNK"]) expect(isFullSession(record(type))).toBe(false);
  });
});

describe("scoped record detail", () => {
  it("hides the previous selection immediately on every scope or filter change", () => {
    const selected = { key: "original", record: record("FULL_SESSION") };
    expect(scopedMemoryRecord(selected, "original")).toBe(selected.record);
    expect(scopedMemoryRecord(null, "original")).toBeNull();
    for (const key of ["workspace", "project", "actor", "strategy", "filter"])
      expect(scopedMemoryRecord(selected, key)).toBeNull();
  });
});

describe("MemoryRequestScope", () => {
  it("rejects a deferred response when actor, session, strategy, filter or scope changes", async () => {
    for (const field of ["actor", "session", "strategy", "recordType", "workspace", "project"]) {
      const scope = new MemoryRequestScope();
      scope.enter("old");
      const current = scope.begin();
      let complete!: (value: string) => void;
      const delivered: string[] = [];
      const pending = new Promise<string>((resolve) => { complete = resolve; })
        .then((value) => { if (current()) delivered.push(value); });
      scope.enter(`new-${field}`);
      complete("stale");
      await pending;
      expect(delivered).toEqual([]);
      expect(scope.begin()()).toBe(true);
    }
  });

  it("ignores earlier same-scope searches, refreshes and post-unmount responses", () => {
    const scope = new MemoryRequestScope();
    scope.enter("session");
    const first = scope.begin();
    const second = scope.begin();
    expect(first()).toBe(false);
    scope.enter("session");
    expect(second()).toBe(true);
    scope.invalidate();
    expect(second()).toBe(false);
    scope.enter("session");
    expect(scope.begin()()).toBe(true);
  });
});

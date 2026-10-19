import { describe, expect, it } from "vitest";

import type { CodingTurnRow } from "../../../lib/api";
import {
  affectsFiles,
  applyCodingEvent,
  applyCodingEvents,
  type CodingEventRecord,
  diffStats,
  emptyConversation,
  liveTurn,
  normalizeUsage,
  OUTPUT_CAP,
  turnPrompt,
  withLedgerTurns,
} from "./codingEvents";

/** Records with consecutive seqs from `start`. */
function log(start: number, ...events: [string, Record<string, unknown>][]): CodingEventRecord[] {
  return events.map(([method, params], i) => ({ seq: start + i, ts: 1_760_000_000_000 + i * 1000, method, params }));
}

const T = "turn-1";
const TH = "thread-1";

/** A full live log of one turn: prompt, reasoning, a message, a command and a file edit. */
function fullTurnLog(): CodingEventRecord[] {
  return log(
    1,
    ["turn/started", { threadId: TH, turn: { id: T, status: "inProgress", items: [], startedAt: 1_760_000_000 } }],
    ["item/started", { threadId: TH, turnId: T, item: { type: "userMessage", id: "u1", content: [{ type: "text", text: "add a test" }] } }],
    ["item/completed", { threadId: TH, turnId: T, item: { type: "userMessage", id: "u1", content: [{ type: "text", text: "add a test" }] } }],
    ["item/started", { turnId: T, item: { type: "reasoning", id: "r1", summary: [], content: [] } }],
    ["item/reasoning/summaryPartAdded", { turnId: T, itemId: "r1", summaryIndex: 0 }],
    ["item/reasoning/summaryTextDelta", { turnId: T, itemId: "r1", delta: "Plan", summaryIndex: 0 }],
    ["item/reasoning/summaryTextDelta", { turnId: T, itemId: "r1", delta: "ning", summaryIndex: 0 }],
    ["item/completed", { turnId: T, item: { type: "reasoning", id: "r1", summary: ["Planning"], content: [] } }],
    ["item/started", { turnId: T, item: { type: "commandExecution", id: "c1", command: "pytest -q", cwd: "/mnt/workspace/project", status: "inProgress", aggregatedOutput: null, exitCode: null, durationMs: null } }],
    ["item/commandExecution/outputDelta", { turnId: T, itemId: "c1", delta: "1 passed\n" }],
    ["item/completed", { turnId: T, item: { type: "commandExecution", id: "c1", command: "pytest -q", cwd: "/mnt/workspace/project", status: "completed", aggregatedOutput: "1 passed\n", exitCode: 0, durationMs: 840 } }],
    ["item/started", { turnId: T, item: { type: "fileChange", id: "f1", changes: [], status: "inProgress" } }],
    ["item/completed", { turnId: T, item: { type: "fileChange", id: "f1", status: "completed", changes: [{ path: "test_app.py", kind: { type: "add" }, diff: "def test():\n    assert True\n" }] } }],
    ["turn/diff/updated", { turnId: T, diff: "diff --git a/x b/x\n" }],
    ["item/started", { turnId: T, item: { type: "agentMessage", id: "a1", text: "" } }],
    ["item/agentMessage/delta", { turnId: T, itemId: "a1", delta: "Added " }],
    ["item/agentMessage/delta", { turnId: T, itemId: "a1", delta: "a test." }],
    ["item/completed", { turnId: T, item: { type: "agentMessage", id: "a1", text: "Added a test." } }],
    ["turn/diff/updated", { turnId: T, diff: "diff --git a/test_app.py b/test_app.py\n" }],
    ["turn/completed", { threadId: TH, turn: { id: T, status: "completed", items: [], error: null, startedAt: 1_760_000_000, completedAt: 1_760_000_042, durationMs: 42_000 } }],
    ["launchpad/turnUsage", { turnId: T, model: "global.openai.gpt-6-sol", usage: { totalTokens: 1200, inputTokens: 1000, cachedInputTokens: 600, cacheWriteInputTokens: 0, outputTokens: 200, reasoningOutputTokens: 50 } }],
  );
}

/** What the runtime's `compact: true` serves: deltas of completed items and all but
 *  the last `turn/diff/updated` per turn dropped; seqs keep their gaps. */
function compact(records: CodingEventRecord[]): CodingEventRecord[] {
  const completed = new Set(
    records.filter((r) => r.method === "item/completed").map((r) => (r.params?.item as { id: string }).id),
  );
  const lastDiff = new Map<string, number>();
  for (const r of records) if (r.method === "turn/diff/updated") lastDiff.set(String(r.params?.turnId), r.seq);
  return records.filter((r) => {
    if (r.method === "turn/diff/updated") return lastDiff.get(String(r.params?.turnId)) === r.seq;
    const isDelta = /\/delta$|Delta$|summaryPartAdded$/.test(r.method);
    return !(isDelta && completed.has(String(r.params?.itemId)));
  });
}

describe("applyCodingEvents", () => {
  it("streams agent text into one item and replaces it on completion", () => {
    const records = fullTurnLog();
    const until = records.findIndex((r) => r.method === "item/agentMessage/delta") + 2;
    const mid = applyCodingEvents(emptyConversation(), records.slice(0, until));
    const turn = mid.turns[0];
    expect(turn.status).toBe("inProgress");
    expect(liveTurn(mid)?.turnId).toBe(T);
    expect(turn.items.find((i) => i.id === "a1")).toEqual({ type: "agentMessage", id: "a1", text: "Added a test.", streaming: true });

    const done = applyCodingEvents(mid, records.slice(until));
    const final = done.turns[0];
    expect(final.items.find((i) => i.id === "a1")).toEqual({ type: "agentMessage", id: "a1", text: "Added a test.", streaming: false });
    expect(final.status).toBe("completed");
    expect(final.durationMs).toBe(42_000);
    expect(final.startedAt).toBe(1_760_000_000_000);
    expect(final.diff).toBe("diff --git a/test_app.py b/test_app.py\n");
    expect(final.usage).toEqual({ input: 1000, cachedInput: 600, cacheWrite: 0, output: 200, reasoningOutput: 50, total: 1200 });
    expect(final.model).toBe("global.openai.gpt-6-sol");
    expect(done.activeTurnId).toBeNull();
    expect(liveTurn(done)).toBeNull();
    expect(done.threadId).toBe(TH);
    expect(turnPrompt(final)).toBe("add a test");
  });

  it("renders a compact replay the same as the live stream", () => {
    const records = fullTurnLog();
    const live = records.reduce(applyCodingEvent, emptyConversation());
    const replay = applyCodingEvents(emptyConversation(), compact(records));
    expect(compact(records).length).toBeLessThan(records.length);
    expect(replay.turns).toEqual(live.turns);
    expect(replay.seq).toBe(live.seq);
  });

  it("ignores a record at or below the applied seq", () => {
    const records = fullTurnLog();
    const once = applyCodingEvents(emptyConversation(), records);
    expect(applyCodingEvents(once, records)).toBe(once);
    // an overlapping reconnect re-delivers the tail: nothing doubles
    const overlap = applyCodingEvents(applyCodingEvents(emptyConversation(), records.slice(0, 17)), records.slice(10));
    expect(overlap.turns).toEqual(once.turns);
    const dup = applyCodingEvents(once, [{ seq: 3, method: "item/agentMessage/delta", params: { turnId: T, itemId: "a1", delta: "x" } }]);
    expect(dup).toBe(once);
  });

  it("keeps command output streamed live and the exit code", () => {
    const records = log(
      1,
      ["turn/started", { turn: { id: T, status: "inProgress" } }],
      ["item/started", { turnId: T, item: { type: "commandExecution", id: "c1", command: "npm test", cwd: "/w", status: "inProgress", aggregatedOutput: null, exitCode: null, durationMs: null } }],
      ["item/commandExecution/outputDelta", { turnId: T, itemId: "c1", delta: "line 1\n" }],
      ["item/commandExecution/outputDelta", { turnId: T, itemId: "c1", delta: "line 2\n" }],
    );
    const live = applyCodingEvents(emptyConversation(), records);
    expect(live.turns[0].items[0]).toMatchObject({ type: "commandExecution", output: "line 1\nline 2\n", status: "inProgress", exitCode: null });
    // a completion without aggregatedOutput keeps the streamed text
    const done = applyCodingEvent(live, {
      seq: 5,
      method: "item/completed",
      params: { turnId: T, item: { type: "commandExecution", id: "c1", command: "npm test", cwd: "/w", status: "failed", aggregatedOutput: null, exitCode: 1, durationMs: 1200 } },
    });
    expect(done.turns[0].items[0]).toMatchObject({ output: "line 1\nline 2\n", status: "failed", exitCode: 1, durationMs: 1200 });
    // late deltas after completion are ignored
    const late = applyCodingEvent(done, { seq: 6, method: "item/commandExecution/outputDelta", params: { turnId: T, itemId: "c1", delta: "late" } });
    expect(late.turns[0].items[0]).toMatchObject({ output: "line 1\nline 2\n" });
  });

  it("keeps the tail of a very long command output", () => {
    const big = "x".repeat(OUTPUT_CAP) + "TAIL";
    const state = applyCodingEvents(
      emptyConversation(),
      log(
        1,
        ["item/started", { turnId: T, item: { type: "commandExecution", id: "c1", command: "yes", status: "inProgress" } }],
        ["item/commandExecution/outputDelta", { turnId: T, itemId: "c1", delta: big }],
      ),
    );
    const item = state.turns[0].items[0];
    expect(item.type === "commandExecution" && item.output.endsWith("TAIL")).toBe(true);
    expect(item.type === "commandExecution" && item.output.length).toBe(OUTPUT_CAP);
    expect(item.type === "commandExecution" && item.truncated).toBe(true);
  });

  it("caps many small output deltas the same within a batch and across batches", () => {
    const chunk = "y".repeat(999) + "\n";
    const count = Math.ceil(OUTPUT_CAP / chunk.length) + 50;
    const deltas: [string, Record<string, unknown>][] = Array.from({ length: count }, (_, i) => [
      "item/commandExecution/outputDelta",
      { turnId: T, itemId: "c1", delta: i === count - 1 ? "LAST\n" : chunk },
    ]);
    const records = log(1, ["item/started", { turnId: T, item: { type: "commandExecution", id: "c1", command: "yes", status: "inProgress" } }], ...deltas);
    const oneBatch = applyCodingEvents(emptyConversation(), records);
    const split = applyCodingEvents(applyCodingEvents(emptyConversation(), records.slice(0, 120)), records.slice(120));
    for (const state of [oneBatch, split]) {
      const item = state.turns[0].items[0];
      expect(item.type).toBe("commandExecution");
      if (item.type !== "commandExecution") continue;
      expect(item.output.length).toBe(OUTPUT_CAP);
      expect(item.output.endsWith("LAST\n")).toBe(true);
      expect(item.truncated).toBe(true);
    }
    expect(split.turns[0].items[0]).toEqual(oneBatch.turns[0].items[0]);
  });

  it("collects reasoning summary parts by index", () => {
    const state = applyCodingEvents(
      emptyConversation(),
      log(
        1,
        ["item/reasoning/summaryTextDelta", { turnId: T, itemId: "r1", delta: "first", summaryIndex: 0 }],
        ["item/reasoning/summaryTextDelta", { turnId: T, itemId: "r1", delta: "second", summaryIndex: 1 }],
        ["item/reasoning/summaryTextDelta", { turnId: T, itemId: "r1", delta: "!", summaryIndex: 1 }],
      ),
    );
    expect(state.turns[0].items[0]).toEqual({ type: "reasoning", id: "r1", summary: ["first", "second!"], content: [], streaming: true });
  });

  it("counts file-change lines from unified diffs and whole-file adds", () => {
    expect(diffStats("update", "@@ -1,2 +1,2 @@\n-a\n+b\n+c\n context\n")).toEqual({ added: 2, removed: 1 });
    expect(diffStats("add", "one\ntwo\n")).toEqual({ added: 2, removed: 0 });
    expect(diffStats("delete", "one\ntwo")).toEqual({ added: 0, removed: 2 });
    const state = applyCodingEvents(emptyConversation(), fullTurnLog());
    const change = state.turns[0].items.find((i) => i.type === "fileChange");
    expect(change).toMatchObject({ status: "completed", changes: [{ path: "test_app.py", kind: "add", added: 2, removed: 0, movePath: null }] });
    const moved = applyCodingEvent(emptyConversation(), {
      seq: 1,
      method: "item/completed",
      params: { turnId: T, item: { type: "fileChange", id: "f2", status: "completed", changes: [{ path: "a.py", kind: { type: "update", move_path: "b.py" }, diff: "@@ -1 +1 @@\n-x\n+y\n" }] } },
    });
    expect(moved.turns[0].items[0]).toMatchObject({ changes: [{ kind: "update", movePath: "b.py", added: 1, removed: 1 }] });
  });

  it("ends interrupted, failed and abandoned turns", () => {
    const base = applyCodingEvents(
      emptyConversation(),
      log(
        1,
        ["turn/started", { turn: { id: T, status: "inProgress" } }],
        ["item/agentMessage/delta", { turnId: T, itemId: "a1", delta: "Working" }],
      ),
    );
    const interrupted = applyCodingEvent(base, { seq: 3, method: "turn/completed", params: { turn: { id: T, status: "interrupted" } } });
    expect(interrupted.turns[0].status).toBe("interrupted");
    expect(interrupted.turns[0].items[0]).toMatchObject({ streaming: false });
    expect(interrupted.activeTurnId).toBeNull();

    const failed = applyCodingEvent(base, {
      seq: 3,
      method: "turn/completed",
      params: { turn: { id: T, status: "failed", error: { message: "model refused" } } },
    });
    expect(failed.turns[0]).toMatchObject({ status: "failed", error: "model refused" });

    const abandoned = applyCodingEvent(base, { seq: 3, method: "launchpad/turnAbandoned", params: { turnId: T } });
    expect(abandoned.turns[0].status).toBe("abandoned");
    expect(abandoned.activeTurnId).toBeNull();
    // an abandon notice never rewrites a turn that already completed
    const late = applyCodingEvent(interrupted, { seq: 4, method: "launchpad/turnAbandoned", params: { turnId: T } });
    expect(late.turns[0].status).toBe("interrupted");
    // nor does a late turn/started re-open it
    const reopened = applyCodingEvent(interrupted, { seq: 5, method: "turn/started", params: { turn: { id: T, status: "inProgress" } } });
    expect(reopened.turns[0].status).toBe("interrupted");
    expect(reopened.activeTurnId).toBeNull();
  });

  it("records error notifications and platform notices", () => {
    const state = applyCodingEvents(
      emptyConversation(),
      log(
        1,
        ["launchpad/restored", {}],
        ["turn/started", { turn: { id: T, status: "inProgress" } }],
        ["error", { turnId: T, willRetry: true, error: { message: "rate limited", additionalDetails: null } }],
        ["launchpad/serverRequestDeclined", { method: "item/commandExecution/requestApproval" }],
        ["account/rateLimits/updated", { anything: true }],
      ),
    );
    expect(state.notices).toEqual([{ id: "notice-1", code: "restored", detail: null, at: 0 }]);
    expect(state.turns[0].items).toEqual([
      { type: "error", id: "error-3", message: "rate limited", willRetry: true },
      { type: "notice", id: "notice-4", code: "serverRequestDeclined", detail: "item/commandExecution/requestApproval" },
    ]);
    expect(state.seq).toBe(5);
  });

  it("reads the runtime's platform events as it sends them", () => {
    const state = applyCodingEvents(
      emptyConversation(),
      log(
        1,
        // emitted by start_turn before the new turn exists: a notice ahead of it
        ["launchpad/threadResumeFailed", { threadId: "th-old", message: "rollout missing" }],
        ["turn/started", { turn: { id: T, status: "inProgress" } }],
        ["launchpad/appServerExited", { code: 137, signal: null }],
        [
          "launchpad/turnAbandoned",
          { turnId: T, reason: "app_server_exited", usage: { inputTokens: 900, cachedInputTokens: 100, outputTokens: 40, totalTokens: 940 } },
        ],
        ["launchpad/turnUsage", { turnId: "turn-2", model: "global.openai.gpt-6-sol", effort: "high", usage: { inputTokens: 1 } }],
      ),
    );
    expect(state.notices).toEqual([{ id: "notice-1", code: "threadResumeFailed", detail: "rollout missing", at: 0 }]);
    expect(state.turns[0].items).toEqual([{ type: "notice", id: "notice-3", code: "appServerExited", detail: "exit 137" }]);
    expect(state.turns[0].status).toBe("abandoned");
    expect(state.turns[0].usage).toMatchObject({ input: 900, cachedInput: 100, output: 40, total: 940 });
    expect(state.turns[1]).toMatchObject({ turnId: "turn-2", model: "global.openai.gpt-6-sol", effort: "high" });
  });

  it("maps tool calls, plans and unknown items", () => {
    const state = applyCodingEvents(
      emptyConversation(),
      log(
        1,
        ["item/completed", { turnId: T, item: { type: "mcpToolCall", id: "m1", server: "aws-mcp", tool: "call_aws", status: "failed", error: { message: "denied" }, durationMs: 30 } }],
        ["turn/plan/updated", { turnId: T, explanation: "steps", plan: [{ step: "write", status: "completed" }, { step: "test", status: "inProgress" }] }],
        ["item/completed", { turnId: T, item: { type: "webSearch", id: "w1", query: "strands docs" } }],
      ),
    );
    const turn = state.turns[0];
    expect(turn.items[0]).toEqual({ type: "mcpToolCall", id: "m1", server: "aws-mcp", tool: "call_aws", status: "failed", error: "denied", durationMs: 30 });
    expect(turn.plan).toEqual({ explanation: "steps", steps: [{ step: "write", status: "completed" }, { step: "test", status: "inProgress" }] });
    expect(turn.items[1]).toEqual({ type: "other", id: "w1", itemType: "webSearch", label: "strands docs" });
  });

  it("never downgrades a completed item on a late item/started", () => {
    const done = applyCodingEvents(emptyConversation(), fullTurnLog());
    const again = applyCodingEvent(done, { seq: 100, method: "item/started", params: { turnId: T, item: { type: "agentMessage", id: "a1", text: "" } } });
    expect(again.turns[0].items.find((i) => i.id === "a1")).toMatchObject({ text: "Added a test.", streaming: false });
  });
});

function row(over: Partial<CodingTurnRow>): CodingTurnRow {
  return {
    id: "row-1",
    turn_id: T,
    prompt: "add a test",
    model: "global.openai.gpt-6-sol",
    effort: "medium",
    status: "running",
    error: null,
    usage: null,
    started_at: "2026-10-19T08:00:00",
    completed_at: null,
    ...over,
  };
}

describe("withLedgerTurns", () => {
  it("fills prompt, model, effort and usage from the ledger", () => {
    const state = applyCodingEvents(emptyConversation(), log(1, ["turn/started", { turn: { id: T, status: "inProgress" } }]));
    const merged = withLedgerTurns(state, [row({ usage: { input: 10, output: 5, cached_input: 2 } })]);
    expect(merged.turns[0]).toMatchObject({
      rowId: "row-1",
      prompt: "add a test",
      model: "global.openai.gpt-6-sol",
      effort: "medium",
      status: "inProgress",
      usage: { input: 10, output: 5, cachedInput: 2, cacheWrite: 0, reasoningOutput: 0, total: 15 },
    });
    expect(withLedgerTurns(merged, [row({ usage: { input: 10, output: 5, cached_input: 2 } })])).toBe(merged);
  });

  it("lets a settled ledger status end a turn the events left in progress", () => {
    const state = applyCodingEvents(
      emptyConversation(),
      log(1, ["turn/started", { turn: { id: T, status: "inProgress" } }], ["item/agentMessage/delta", { turnId: T, itemId: "a1", delta: "hi" }]),
    );
    const merged = withLedgerTurns(state, [row({ status: "abandoned", completed_at: "2026-10-19T16:10:00" })]);
    expect(merged.turns[0].status).toBe("abandoned");
    expect(merged.turns[0].items[0]).toMatchObject({ streaming: false });
    expect(merged.activeTurnId).toBeNull();
    // but a stale "running" row never re-opens a completed turn
    const done = applyCodingEvent(state, { seq: 3, method: "turn/completed", params: { turn: { id: T, status: "completed" } } });
    expect(withLedgerTurns(done, [row({ status: "running" })]).turns[0].status).toBe("completed");
  });

  it("adds a just-started ledger turn and places ledger-only turns by start time", () => {
    const optimistic = withLedgerTurns(emptyConversation(), [row({ turn_id: "t-new" })]);
    expect(optimistic.turns.map((t) => t.turnId)).toEqual(["t-new"]);
    expect(optimistic.activeTurnId).toBe("t-new");
    expect(turnPrompt(optimistic.turns[0])).toBe("add a test");

    const state = applyCodingEvents(
      emptyConversation(),
      log(
        1,
        ["turn/started", { turn: { id: "t1", status: "inProgress", startedAt: Date.parse("2026-10-19T08:00:00Z") / 1000 } }],
        ["turn/completed", { turn: { id: "t1", status: "completed" } }],
        ["turn/started", { turn: { id: "t3", status: "inProgress", startedAt: Date.parse("2026-10-19T10:00:00Z") / 1000 } }],
      ),
    );
    const merged = withLedgerTurns(state, [row({ id: "r2", turn_id: "t2", status: "abandoned", started_at: "2026-10-19T09:00:00" })]);
    expect(merged.turns.map((t) => t.turnId)).toEqual(["t1", "t2", "t3"]);
    expect(merged.turns[1].status).toBe("abandoned");
    expect(merged.activeTurnId).toBe("t3");
  });
});

describe("helpers", () => {
  it("normalizes both usage shapes", () => {
    expect(normalizeUsage(null)).toBeNull();
    expect(normalizeUsage({})).toBeNull();
    expect(normalizeUsage({ input: 3, output: 4 })).toMatchObject({ input: 3, output: 4, total: 7 });
    expect(normalizeUsage({ inputTokens: 3, outputTokens: 4, totalTokens: 9 })).toMatchObject({ total: 9 });
  });

  it("flags the records that may change files", () => {
    expect(affectsFiles({ seq: 1, method: "turn/completed", params: {} })).toBe(true);
    expect(affectsFiles({ seq: 1, method: "item/completed", params: { item: { type: "fileChange" } } })).toBe(true);
    expect(affectsFiles({ seq: 1, method: "item/completed", params: { item: { type: "agentMessage" } } })).toBe(false);
    expect(affectsFiles({ seq: 1, method: "item/agentMessage/delta", params: {} })).toBe(false);
  });
});

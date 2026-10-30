import { describe, expect, it } from "vitest";

import { ApiError, type AssistantConversationDetail } from "./api";
import type { AssistantLiveMessage } from "./assistant";
import {
  applyTurnEvent,
  giveUpMessages,
  markReconnecting,
  reattachPlan,
  reconnectDelay,
  runTurnLink,
  TURN_RECONNECT_CAP_MS,
  type TurnEvent,
  type TurnLinkStatus,
} from "./assistant-turn";

/**
 * N-ARCH-obs15: the connection is not the turn. A stream that closes without a
 * terminal event is a reconnect (from the last seen `seq`), never an interruption; the
 * link gives up only once the server's claim TTL has passed; an abort by the page
 * (scope change, unmount) ends the subscription and nothing else.
 */

const frame = (event: string, data: unknown, seq?: number) =>
  `${seq !== undefined ? `id: ${seq}\n` : ""}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

/** An SSE response body made of the given frames; `hang` keeps it open until aborted. */
function sse(frames: string[], opts: { hang?: boolean; signal?: AbortSignal } = {}): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(encoder.encode(f));
      if (!opts.hang) controller.close();
      else opts.signal?.addEventListener("abort", () => controller.error(new DOMException("aborted", "AbortError")));
    },
  });
  return new Response(stream, { headers: { "content-type": "text/event-stream" } });
}

const noWait = async () => undefined;
const translate = (code: string | null, msg: string) => (code ? `[${code}] ${msg}` : msg);

describe("reconnectDelay", () => {
  it("doubles from 1 s and caps at 30 s", () => {
    expect([0, 1, 2, 3, 4, 5, 6].map(reconnectDelay)).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000]);
    expect(reconnectDelay(99)).toBe(TURN_RECONNECT_CAP_MS);
  });
});

describe("runTurnLink", () => {
  it("close without a terminal event → reconnecting → reattached from the last seq → done", async () => {
    const statuses: [TurnLinkStatus, number][] = [];
    const events: TurnEvent[] = [];
    const reattaches: [number, number][] = [];
    const outcome = await runTurnLink({
      open: async () => sse([frame("meta", { turn: 3 }, 1), frame("delta", { text: "hel" }, 2)]),
      reattach: async (turn, from) => {
        reattaches.push([turn, from]);
        return sse([frame("delta", { text: "lo" }, 3), frame("proposal", { status: "draft", revision: 1 }, 4), frame("done", { turn: 3 }, 5)]);
      },
      signal: new AbortController().signal,
      onEvent: (e) => events.push(e),
      onStatus: (s, a) => statuses.push([s, a]),
      wait: noWait,
    });
    expect(reattaches).toEqual([[3, 2]]);
    expect(statuses).toEqual([["reconnecting", 0], ["reattached", 1]]);
    expect(events.map((e) => e.event)).toEqual(["meta", "delta", "delta", "proposal", "done"]);
    expect(outcome).toMatchObject({ kind: "done", turn: 3, lastSeq: 5, completed: true, error: null });
    expect(outcome.proposal).toMatchObject({ revision: 1 });
  });

  it("retries a failed reattach with exponential backoff and keeps the seq", async () => {
    const waits: number[] = [];
    let calls = 0;
    const outcome = await runTurnLink({
      open: async () => sse([frame("meta", { turn: 1 }, 1), frame("delta", { text: "a" }, 2)]),
      reattach: async (_turn, from) => {
        calls += 1;
        if (calls < 4) throw new TypeError("Failed to fetch");
        expect(from).toBe(2);
        return sse([frame("done", { turn: 1 }, 3)]);
      },
      signal: new AbortController().signal,
      onEvent: () => undefined,
      wait: async (ms) => { waits.push(ms); },
    });
    expect(waits).toEqual([1000, 2000, 4000, 8000]);
    expect(outcome.kind).toBe("done");
  });

  it("gives up once the claim TTL has passed since the first disconnect", async () => {
    let clock = 0;
    const statuses: TurnLinkStatus[] = [];
    const outcome = await runTurnLink({
      open: async () => sse([frame("meta", { turn: 1 }, 1)]),
      reattach: async () => { throw new TypeError("offline"); },
      signal: new AbortController().signal,
      onEvent: () => undefined,
      onStatus: (s) => statuses.push(s),
      wait: async (ms) => { clock += ms; },
      now: () => clock,
      giveUpMs: 5000,
    });
    expect(outcome.kind).toBe("gave_up");
    expect(outcome.turn).toBe(1);
    expect(statuses.every((s) => s === "reconnecting")).toBe(true);
    expect(statuses.length).toBeGreaterThan(1);
  });

  it("an abort ends the subscription only — no error, no give-up", async () => {
    const controller = new AbortController();
    let reattached = 0;
    const promise = runTurnLink({
      open: async () => sse([frame("meta", { turn: 1 }, 1)]),
      reattach: async (_turn, _from, signal) => {
        reattached += 1;
        return sse([frame("delta", { text: "more" }, 2)], { hang: true, signal }); // a live tail
      },
      signal: controller.signal,
      onEvent: () => undefined,
      wait: noWait,
    });
    setTimeout(() => controller.abort(), 20); // the page bumps its generation / unmounts
    const outcome = await promise;
    expect(outcome).toMatchObject({ kind: "aborted", turn: 1, lastSeq: 2 });
    expect(reattached).toBe(1);
  });

  it("an abort during the backoff sleep reattaches nothing", async () => {
    const controller = new AbortController();
    let reattached = 0;
    const promise = runTurnLink({
      open: async () => sse([frame("meta", { turn: 1 }, 1)]),
      reattach: async () => { reattached += 1; throw new TypeError("offline"); },
      signal: controller.signal,
      onEvent: () => undefined,
      wait: (_ms, signal) => new Promise((r) => signal.addEventListener("abort", () => r())),
    });
    setTimeout(() => controller.abort(), 5);
    const outcome = await promise;
    expect(outcome.kind).toBe("aborted");
    expect(reattached).toBe(0);
  });

  it("a refusal of the reattach itself is final", async () => {
    await expect(runTurnLink({
      open: async () => sse([frame("meta", { turn: 2 }, 1)]),
      reattach: async () => { throw new ApiError("assistant.turn_not_yours", "another member", null); },
      signal: new AbortController().signal,
      onEvent: () => undefined,
      wait: noWait,
    })).rejects.toMatchObject({ code: "assistant.turn_not_yours" });
  });

  it("the initial POST being refused throws as before", async () => {
    await expect(runTurnLink({
      open: async () => { throw new ApiError("assistant.turn_in_progress", "busy", null); },
      reattach: async () => sse([]),
      signal: new AbortController().signal,
      onEvent: () => undefined,
      wait: noWait,
    })).rejects.toMatchObject({ code: "assistant.turn_in_progress" });
  });

  it("an error event is terminal", async () => {
    const outcome = await runTurnLink({
      open: async () => sse([frame("meta", { turn: 1 }, 1), frame("error", { code: "harness.execution_timeout", message: "t/o" }, 2)]),
      reattach: async () => { throw new Error("must not reattach after a terminal event"); },
      signal: new AbortController().signal,
      onEvent: () => undefined,
      wait: noWait,
    });
    expect(outcome.kind).toBe("error");
    expect(outcome.error?.code).toBe("harness.execution_timeout");
  });

  it("page-load reattach: open is the reattach itself, from the plan's turn", async () => {
    const outcome = await runTurnLink({
      turn: 4,
      fromSeq: 0,
      open: async () => sse([frame("meta", { turn: 4 }, 1), frame("delta", { text: "x" }, 2), frame("done", { turn: 4 }, 3)]),
      reattach: async () => { throw new Error("unused"); },
      signal: new AbortController().signal,
      onEvent: () => undefined,
      wait: noWait,
    });
    expect(outcome).toMatchObject({ kind: "done", turn: 4, lastSeq: 3 });
  });
});

describe("applyTurnEvent / transcript helpers", () => {
  const base: AssistantLiveMessage[] = [
    { role: "user", text: "hi" },
    { role: "assistant", text: "", streaming: true },
  ];

  it("appends deltas to the streaming row and clears its reconnecting flag", () => {
    const marked = markReconnecting(base, true);
    expect(marked[1].reconnecting).toBe(true);
    const next = applyTurnEvent(marked, { event: "delta", data: { text: "he" } as never, seq: 2 }, translate);
    expect(next[1]).toEqual({ role: "assistant", text: "he", streaming: true, reconnecting: false });
  });

  it("tool, tool_input, retry and invalid proposal", () => {
    let m = applyTurnEvent(base, { event: "tool", data: { name: "lookup", id: "t1" } as never }, translate);
    expect(m.map((x) => x.role)).toEqual(["user", "tool", "assistant"]);
    m = applyTurnEvent(m, { event: "tool_input", data: { id: "t1", input: "{\"q\":1}" } as never }, translate);
    expect(m[1].text).toBe("{\"q\":1}");
    m = applyTurnEvent(m, { event: "delta", data: { text: "partial" } as never }, translate);
    m = applyTurnEvent(m, { event: "retry", data: { attempt: 1, reason: "boom" } as never }, translate);
    expect(m).toEqual([{ role: "user", text: "hi" }, { role: "assistant", text: "", streaming: true }]);
    m = applyTurnEvent(m, { event: "proposal", data: { status: "invalid", validation_errors: ["name: bad"] } as never }, translate);
    expect(m[m.length - 1]).toEqual({ role: "error", name: "proposal_rejected", text: "- name: bad" });
  });

  it("error replaces an empty streaming row with the translated error", () => {
    const m = applyTurnEvent(base, { event: "error", data: { code: "assistant.turn_superseded", message: "taken" } as never }, translate);
    expect(m).toEqual([{ role: "user", text: "hi" }, { role: "error", text: "[assistant.turn_superseded] taken" }]);
  });

  it("giveUpMessages closes the streaming row and shows the interrupted marker", () => {
    const m = giveUpMessages([{ role: "user", text: "hi" }, { role: "assistant", text: "so far", streaming: true, reconnecting: true }]);
    expect(m).toEqual([
      { role: "user", text: "hi" },
      { role: "assistant", text: "so far", streaming: false, reconnecting: false },
      { role: "error", name: "turn_interrupted", text: "interrupted" },
    ]);
  });
});

describe("reattachPlan", () => {
  const detail = (live: AssistantConversationDetail["live_turn"]): AssistantConversationDetail =>
    ({
      id: "c1",
      live_turn: live,
      messages: [
        { id: 1, turn: 1, role: "user", text: "first", name: null, author: "river", at: null },
        { id: 2, turn: 1, role: "assistant", text: "answer", name: null, author: null, at: null },
        { id: 3, turn: 2, role: "user", text: "second", name: null, author: "river", at: null },
        { id: 4, turn: 2, role: "tool", text: "", name: "lookup", author: null, at: null },
      ],
    }) as unknown as AssistantConversationDetail;

  it("null without a live turn or for another member's turn", () => {
    expect(reattachPlan(detail(null), "river")).toBeNull();
    expect(reattachPlan(detail({ turn: 2, seq: 3, started_at: null, state: "running", author: "sam" }), "river")).toBeNull();
  });

  it("drops the live turn's non-user rows (the replay rebuilds them) and appends a streaming row", () => {
    const plan = reattachPlan(detail({ turn: 2, seq: 3, started_at: null, state: "running", author: "river" }), "river");
    expect(plan?.turn).toBe(2);
    expect(plan?.messages.map((m) => [m.role, m.text])).toEqual([
      ["user", "first"], ["assistant", "answer"], ["user", "second"], ["assistant", ""],
    ]);
    expect(plan?.messages[3].streaming).toBe(true);
    // an unknown viewer still reattaches (the server answers 409 for another member's turn)
    expect(reattachPlan(detail({ turn: 2, seq: 3, started_at: null, state: "running", author: "sam" }), null)?.turn).toBe(2);
    expect(reattachPlan(detail({ turn: 2, seq: 3, started_at: null, state: "running", author: "river" }), "river")?.watch).toBe(false);
  });

  it("tails a read-only viewer's foreign live turn only as a watcher, and only when asked to", () => {
    const live = { turn: 2, seq: 3, started_at: null, state: "running" as const, author: "sam" };
    const readOnly = { ...detail(live), read_only: true } as AssistantConversationDetail;
    // the default (classic console) never attaches to another member's turn
    expect(reattachPlan(readOnly, "river")).toBeNull();
    // the V2 page opts in: the turn is tailed as `watch` (no busy state, no sender role)
    const plan = reattachPlan(readOnly, "river", { watchReadOnly: true });
    expect(plan?.turn).toBe(2);
    expect(plan?.watch).toBe(true);
    expect(plan?.messages.map((m) => [m.role, m.text])).toEqual([
      ["user", "first"], ["assistant", "answer"], ["user", "second"], ["assistant", ""],
    ]);
    // a collaborator on a shared conversation is not read-only: the server would answer
    // 409 turn_not_yours, so the page does not attach at all
    expect(reattachPlan(detail(live), "river", { watchReadOnly: true })).toBeNull();
    // the viewer's own turn is a reattach, never a watch
    expect(reattachPlan({ ...detail({ ...live, author: "river" }), read_only: false } as AssistantConversationDetail,
      "river", { watchReadOnly: true })?.watch).toBe(false);
  });
});

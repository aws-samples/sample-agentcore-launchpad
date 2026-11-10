import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api";
import type { TurnEvent, TurnLinkStatus } from "./assistant-turn";
import {
  appendDelta,
  chatReattachPlan,
  giveUpChatMessages,
  isFinalChatReattachError,
  markChatReconnecting,
  runChatTurnLink,
} from "./chat-turn";

/**
 * N-ARCH-obs15 v2 (chat parity): the Chat pages drive the shared turn link with the chat
 * reattach endpoint and refusal codes. A stream that closes without a terminal event is a
 * reconnect from the last seen `seq`, never an interruption; the session id learnt from
 * `meta` is what the reattach uses; refusals of the reattach are final; the pure helpers
 * mark / unmark the open bubble and plan a page-load reattach from `history.live_turn`.
 */

const frame = (event: string, data: unknown, seq?: number) =>
  `${seq !== undefined ? `id: ${seq}\n` : ""}event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

function sse(frames: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(encoder.encode(f));
      controller.close();
    },
  });
  return new Response(stream, { headers: { "content-type": "text/event-stream" } });
}

const noWait = async () => undefined;
const SID = "s".repeat(40);

afterEach(() => vi.unstubAllGlobals());

describe("runChatTurnLink", () => {
  it("reattaches through GET …/sessions/{sid}/turns/{turn}/stream?from= with the session learnt from meta", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
      calls.push(String(input));
      return sse([frame("delta", { text: "lo" }, 3), frame("done", { latency_ms: 5 }, 4)]);
    });
    const statuses: [TurnLinkStatus, number][] = [];
    const events: TurnEvent[] = [];
    let sessionId: string | null = null;
    const outcome = await runChatTurnLink({
      agentId: "agent-1",
      sessionId: () => sessionId,
      open: async () => sse([
        frame("meta", { session_id: SID, agent: "a", mode: "stream", turn: 2 }, 1),
        frame("delta", { text: "hel" }, 2),
      ]),
      signal: new AbortController().signal,
      onEvent: (e) => {
        events.push(e);
        const data = e.data as { session_id?: string };
        if (e.event === "meta" && data.session_id) sessionId = data.session_id;
      },
      onStatus: (s, a) => statuses.push([s, a]),
      wait: noWait,
    });
    expect(calls).toEqual([`/api/chat/agent-1/sessions/${SID}/turns/2/stream?from=2`]);
    expect(statuses).toEqual([["reconnecting", 0], ["reattached", 1]]);
    expect(events.map((e) => e.event)).toEqual(["meta", "delta", "delta", "done"]);
    expect(outcome).toMatchObject({ kind: "done", turn: 2, lastSeq: 4, completed: true });
  });

  it("a refusal of the chat reattach is final; a network failure is retried", async () => {
    let calls = 0;
    vi.stubGlobal("fetch", async () => {
      calls += 1;
      if (calls === 1) throw new TypeError("Failed to fetch");
      return new Response(JSON.stringify({ code: "chat.turn_not_yours", message: "no", detail: { author: "pat" } }), {
        status: 409,
        headers: { "content-type": "application/json" },
      });
    });
    await expect(
      runChatTurnLink({
        agentId: "agent-1",
        sessionId: () => SID,
        open: async () => sse([frame("meta", { session_id: SID, turn: 1 }, 1)]),
        signal: new AbortController().signal,
        onEvent: () => undefined,
        wait: noWait,
      }),
    ).rejects.toMatchObject({ code: "chat.turn_not_yours" });
    expect(calls).toBe(2);
  });

  it("a close before meta is a lost chat turn", async () => {
    await expect(
      runChatTurnLink({
        agentId: "agent-1",
        sessionId: () => null,
        open: async () => sse([]),
        signal: new AbortController().signal,
        onEvent: () => undefined,
        wait: noWait,
      }),
    ).rejects.toMatchObject({ code: "chat.turn_lost" });
  });

  it("the initial POST being refused throws as before", async () => {
    await expect(
      runChatTurnLink({
        agentId: "agent-1",
        sessionId: () => null,
        open: async () => {
          throw new Error("409 a turn of this session is still streaming");
        },
        signal: new AbortController().signal,
        onEvent: () => undefined,
        wait: noWait,
      }),
    ).rejects.toThrow(/still streaming/);
  });

  it("isFinalChatReattachError: chat refusals and auth are final, anything else retries", () => {
    for (const code of ["chat.turn_not_found", "chat.turn_not_yours", "chat.session_not_found", "agent.not_found", "http.401"]) {
      expect(isFinalChatReattachError(new ApiError(code, "", null))).toBe(true);
    }
    expect(isFinalChatReattachError(new ApiError("http.502", "", null))).toBe(false);
    expect(isFinalChatReattachError(new TypeError("Failed to fetch"))).toBe(false);
  });
});

type Row = { kind: string; text: string; streaming?: boolean; reconnecting?: boolean };

describe("transcript helpers", () => {
  it("markChatReconnecting flags the open bubble, skipping cards after it, and unflags it", () => {
    const rows: Row[] = [
      { kind: "user", text: "q" },
      { kind: "agent", text: "half", streaming: true },
      { kind: "auth", text: "github" },
    ];
    const marked = markChatReconnecting(rows, true);
    expect(marked[1]).toMatchObject({ reconnecting: true, streaming: true, text: "half" });
    expect(marked[2]).toEqual(rows[2]);
    expect(markChatReconnecting(marked, true)).toBe(marked); // idempotent
    expect(markChatReconnecting(marked, false)[1]).toMatchObject({ reconnecting: false, text: "half" });
  });

  it("markChatReconnecting adds an empty placeholder before the first delta and removes it again", () => {
    const rows: Row[] = [{ kind: "user", text: "q" }];
    const marked = markChatReconnecting(rows, true);
    expect(marked).toHaveLength(2);
    expect(marked[1]).toMatchObject({ kind: "agent", text: "", streaming: true, reconnecting: true });
    expect(markChatReconnecting(marked, false)).toEqual(rows);
    expect(markChatReconnecting(rows, false)).toBe(rows);
  });

  it("giveUpChatMessages closes streaming bubbles, drops an empty one and appends the copy", () => {
    const rows: Row[] = [
      { kind: "user", text: "q" },
      { kind: "agent", text: "half", streaming: true, reconnecting: true },
      { kind: "agent", text: "", streaming: true, reconnecting: true },
    ];
    expect(giveUpChatMessages(rows, "interrupted")).toEqual([
      { kind: "user", text: "q" },
      { kind: "agent", text: "half", streaming: false, reconnecting: false },
      { kind: "error", text: "interrupted" },
    ]);
  });
});

describe("chatReattachPlan", () => {
  const live = { turn: 2, seq: 3, started_at: null, state: "running" as const, author: "river" };
  const rows: Row[] = [
    { kind: "user", text: "first" },
    { kind: "agent", text: "one" },
    { kind: "user", text: "second" },
    { kind: "agent", text: "partial" },
    { kind: "tool", text: "search" },
  ];

  it("null without a live turn or for another member's turn", () => {
    expect(chatReattachPlan(rows, null, "river")).toBeNull();
    expect(chatReattachPlan(rows, undefined, "river")).toBeNull();
    expect(chatReattachPlan(rows, live, "pat")).toBeNull();
  });

  it("keeps the rows up to the live turn's user row (the replay rebuilds the rest) and adds no placeholder", () => {
    const plan = chatReattachPlan(rows, live, "river");
    expect(plan?.turn).toBe(2);
    expect(plan?.messages).toEqual([
      { kind: "user", text: "first" },
      { kind: "agent", text: "one" },
      { kind: "user", text: "second" },
    ]);
    expect(plan?.messages).toHaveLength(3);
    // an unknown viewer reattaches too (the server refuses another member's turn itself)
    expect(chatReattachPlan(rows, live, null)?.turn).toBe(2);
    // a thread with no user row yet (nothing persisted before the reattach) is kept whole
    expect(chatReattachPlan([], live, "river")?.messages).toEqual([]);
  });
});

/**
 * rev-r11 FIX: a page-load reattach replays the turn from seq 0 through the SAME reducer
 * steps both Chat pages run in `consumeTurn` — the open flag starts false, a `tool` row is
 * pushed and clears it, `delta` goes through `appendDelta`, `done` adds no row and the
 * finally closes every streaming row. The plan used to append an empty streaming bubble
 * that the first replayed `delta` did not adopt (open was false), leaving an empty agent
 * row above the real answer; these drive the exact sequence and pin the row count.
 */
describe("page-load reattach replay (plan → the pages' reducer steps)", () => {
  const live = { turn: 2, seq: 3, started_at: null, state: "running" as const, author: "river" };
  const history: Row[] = [
    { kind: "user", text: "first" },
    { kind: "agent", text: "one" },
    { kind: "user", text: "second" },
    { kind: "agent", text: "part" }, // what had streamed before the reload — rebuilt by the replay
  ];
  type Frame = { event: string; text?: string; name?: string };

  /** `consumeTurn`'s transcript steps for the events a replay carries (both pages). */
  function replay(initial: Row[], frames: Frame[]): Row[] {
    let messages = initial;
    let open = false;
    for (const f of frames) {
      if (f.event === "tool") {
        messages = [...messages, { kind: "tool", text: f.name ?? "tool" }];
        open = false;
      } else if (f.event === "delta") {
        messages = appendDelta(messages, f.text ?? "", open);
        open = true;
      }
      // meta / done: no transcript row
    }
    // the finally of attachLive / send
    return messages.map((m) => (m.streaming ? { ...m, streaming: false, reconnecting: false } : m));
  }

  it("meta, delta, delta, done → exactly one agent row holding the whole answer", () => {
    const plan = chatReattachPlan(history, live, "river");
    expect(plan).not.toBeNull();
    const out = replay(plan!.messages, [
      { event: "meta" },
      { event: "delta", text: "The answer " },
      { event: "delta", text: "is 42." },
      { event: "done" },
    ]);
    expect(out).toHaveLength(4);
    expect(out.map((m) => [m.kind, m.text])).toEqual([
      ["user", "first"],
      ["agent", "one"],
      ["user", "second"],
      ["agent", "The answer is 42."],
    ]);
    expect(out.filter((m) => m.kind === "agent" && !m.text)).toHaveLength(0);
  });

  it("meta, tool, delta, done → the tool row directly under the user row, then one agent row", () => {
    const plan = chatReattachPlan(history, live, "river");
    const out = replay(plan!.messages, [
      { event: "meta" },
      { event: "tool", name: "search" },
      { event: "delta", text: "Found it." },
      { event: "done" },
    ]);
    expect(out).toHaveLength(5);
    expect(out.map((m) => [m.kind, m.text])).toEqual([
      ["user", "first"],
      ["agent", "one"],
      ["user", "second"],
      ["tool", "search"],
      ["agent", "Found it."],
    ]);
  });

  it("a reconnect before the first delta leaves nothing behind either", () => {
    // the link marks reconnecting (empty placeholder), then reports reattached BEFORE the
    // replayed events are dispatched (assistant-turn.ts), which removes the placeholder
    const plan = chatReattachPlan(history, live, "river");
    let m = markChatReconnecting(plan!.messages, true);
    expect(m).toHaveLength(4);
    m = markChatReconnecting(m, false);
    const out = replay(m, [{ event: "delta", text: "late" }, { event: "done" }]);
    expect(out.map((x) => [x.kind, x.text])).toEqual([
      ["user", "first"],
      ["agent", "one"],
      ["user", "second"],
      ["agent", "late"],
    ]);
  });
});

describe("appendDelta (shared by the classic and V2 pages)", () => {
  it("pushes a streaming bubble when none is open, appends while open, skips cards", () => {
    let m: Row[] = [{ kind: "user", text: "q" }];
    m = appendDelta(m, "a", false);
    m = [...m, { kind: "auth", text: "github" }, { kind: "policy", text: "denied" }];
    m = appendDelta(m, "b", true);
    expect(m.map((x) => [x.kind, x.text])).toEqual([
      ["user", "q"],
      ["agent", "ab"],
      ["auth", "github"],
      ["policy", "denied"],
    ]);
    expect(m[1]).toMatchObject({ streaming: true });
  });

  it("never appends to a finished bubble, even with the open flag set", () => {
    const m = appendDelta([{ kind: "agent", text: "old" }] as Row[], "new", true);
    expect(m.map((x) => x.text)).toEqual(["old", "new"]);
  });
});

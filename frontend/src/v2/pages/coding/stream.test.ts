import { describe, expect, it } from "vitest";

import {
  afterStream,
  HIDDEN_PAUSE_MS,
  isTerminalStreamError,
  nextDelay,
  parseSseFrame,
  resumeCursor,
  shouldPause,
  sseFrameBatches,
} from "./stream";

/** A streamed body that arrives in exactly these chunks. */
function streamed(chunks: string[]): Response {
  const encoder = new TextEncoder();
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
        controller.close();
      },
    }),
  );
}

describe("parseSseFrame", () => {
  it("reads event, id and JSON data", () => {
    expect(parseSseFrame('id: 7\nevent: event\ndata: {"seq":7,"method":"turn/started"}')).toEqual({
      event: "event",
      id: "7",
      data: { seq: 7, method: "turn/started" },
    });
  });

  it("keeps a frame without data (reconnect) and skips comment-only frames", () => {
    expect(parseSseFrame("event: reconnect")).toEqual({ event: "reconnect", id: null, data: null });
    expect(parseSseFrame(": keep-alive")).toBeNull();
    expect(parseSseFrame("")).toBeNull();
  });

  it("joins multi-line data and survives bad JSON", () => {
    expect(parseSseFrame('data: {"a":\ndata: 1}')).toMatchObject({ event: "message", data: { a: 1 } });
    expect(parseSseFrame("event: error\ndata: not json")).toEqual({ event: "error", id: null, data: null });
    expect(parseSseFrame("event: status\r\ndata: {}\r")).toEqual({ event: "status", id: null, data: {} });
  });
});

describe("sseFrameBatches", () => {
  it("yields one batch per chunk, across split frames and CRLF", async () => {
    const res = streamed([
      'event: status\ndata: {"project_status":"running"}\n\n: keep-alive\n\nid: 1\nevent: event\ndata: {"seq":1}\n\nid: 2\nevent: ev',
      'ent\ndata: {"seq":2}\n\n',
      "event: reconnect\r\n\r\n",
    ]);
    const batches = [];
    for await (const batch of sseFrameBatches(res)) batches.push(batch.map((f) => [f.event, f.id, f.data]));
    expect(batches).toEqual([
      [
        ["status", null, { project_status: "running" }],
        ["event", "1", { seq: 1 }],
      ],
      [["event", "2", { seq: 2 }]],
      [["reconnect", null, null]],
    ]);
  });

  it("flushes a final frame without a trailing blank line", async () => {
    const batches = [];
    for await (const batch of sseFrameBatches(streamed(['id: 3\nevent: event\ndata: {"seq":3}']))) batches.push(batch);
    expect(batches).toHaveLength(1);
    expect(batches[0][0].data).toEqual({ seq: 3 });
  });
});

describe("reconnect policy", () => {
  it("backs off 1 s, 2 s, 5 s, then caps at 10 s", () => {
    expect([0, 1, 2, 3, 4, 9].map(nextDelay)).toEqual([0, 1000, 2000, 5000, 10_000, 10_000]);
  });

  it("reconnects at once on a server reconnect and resets the failures", () => {
    expect(afterStream("reconnect", 3, false)).toEqual({ delayMs: 0, failures: 0 });
  });

  it("counts consecutive failures, and an event resets the count", () => {
    let state = { delayMs: 0, failures: 0 };
    state = afterStream("failed", state.failures, false);
    expect(state).toEqual({ delayMs: 1000, failures: 1 });
    state = afterStream("failed", state.failures, false);
    expect(state).toEqual({ delayMs: 2000, failures: 2 });
    state = afterStream("error", state.failures, false);
    expect(state).toEqual({ delayMs: 5000, failures: 3 });
    expect(afterStream("failed", 3, true)).toEqual({ delayMs: 1000, failures: 1 });
    // a stream that delivered events and just ended is a rotation
    expect(afterStream("ended", 3, true)).toEqual({ delayMs: 250, failures: 1 });
    expect(afterStream("ended", 0, false)).toEqual({ delayMs: 1000, failures: 1 });
  });

  it("pauses a tab hidden for over a minute", () => {
    expect(shouldPause(null, 10_000)).toBe(false);
    expect(shouldPause(0, HIDDEN_PAUSE_MS - 1)).toBe(false);
    expect(shouldPause(0, HIDDEN_PAUSE_MS)).toBe(true);
  });

  it("resumes after the highest applied seq", () => {
    expect(resumeCursor(5, null)).toBe(5);
    expect(resumeCursor(5, "9")).toBe(9);
    expect(resumeCursor(5, "3")).toBe(5);
    expect(resumeCursor(5, "x")).toBe(5);
  });
});

describe("isTerminalStreamError", () => {
  it("stops on a gone project or runtime, retries everything else", () => {
    expect(isTerminalStreamError("coding.project_not_found")).toBe(true);
    expect(isTerminalStreamError("coding.runtime_not_installed")).toBe(true);
    expect(isTerminalStreamError("http.401")).toBe(true);
    // the auth middleware's envelope codes, not only bare statuses
    expect(isTerminalStreamError("auth.required")).toBe(true);
    expect(isTerminalStreamError("auth.permission_required")).toBe(true);
    expect(isTerminalStreamError("coding.runtime_error")).toBe(false);
    expect(isTerminalStreamError("http.502")).toBe(false);
    expect(isTerminalStreamError(null)).toBe(false);
  });
});

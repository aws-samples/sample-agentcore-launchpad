import { describe, expect, it } from "vitest";

import { asUserChecked, asUserField, sseEvents } from "./chat";

describe("invoke as me", () => {
  it("omits as_user until the member toggles it, so the backend picks", () => {
    expect(asUserField(true, null)).toBeUndefined();
    expect(asUserField(true, true)).toBe(true);
    expect(asUserField(true, false)).toBe(false);
  });

  it("never sends as_user for a non-JWT agent", () => {
    expect(asUserField(false, null)).toBeUndefined();
    expect(asUserField(false, true)).toBeUndefined();
    expect(asUserField(false, false)).toBeUndefined();
  });

  it("shows auto as checked only when signed in through the pool", () => {
    expect(asUserChecked(null, true)).toBe(true);
    expect(asUserChecked(null, false)).toBe(false);
    expect(asUserChecked(true, false)).toBe(true);
    expect(asUserChecked(false, true)).toBe(false);
  });
});

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

async function collect<T>(res: Response): Promise<{ event: string; data: T }[]> {
  const out: { event: string; data: T }[] = [];
  for await (const frame of sseEvents<T>(res)) out.push(frame);
  return out;
}

describe("sseEvents", () => {
  it("skips keep-alive comments and joins a frame split across chunks", async () => {
    const res = streamed([
      ": keep-alive\n\n",
      'event: step\ndata: {"name":"T2",',
      '"status":"success"}\n\n: keep-alive\n\n',
      'event: done\ndata: {"answer":"ok"}\n\n',
    ]);
    expect(await collect<Record<string, unknown>>(res)).toEqual([
      { event: "step", data: { name: "T2", status: "success" } },
      { event: "done", data: { answer: "ok" } },
    ]);
  });

  it("reads Chat's frames exactly as before (default event name, no trailing frame)", async () => {
    const res = streamed(['data: {"text":"hi"}\n\n', 'event: done\ndata: {"text":""}\n\n', 'data: {"cut":']);
    expect(await collect(res)).toEqual([
      { event: "message", data: { text: "hi" } },
      { event: "done", data: { text: "" } },
    ]);
  });
});

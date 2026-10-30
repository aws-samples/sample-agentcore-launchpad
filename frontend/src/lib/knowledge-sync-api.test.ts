import { afterEach, describe, expect, it, vi } from "vitest";

import { v2KnowledgeApi } from "./api";

/** N17-obs272: the full rebuild is the sync route with `{full: true}`; the plain sync sends no body. */
describe("v2KnowledgeApi.sync body (N17-obs272)", () => {
  afterEach(() => vi.unstubAllGlobals());

  function stubFetch() {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ job_id: "ij1", status: "STARTING" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }));
    return calls;
  }

  it("POSTs {full: true} for the full rebuild", async () => {
    const calls = stubFetch();
    await v2KnowledgeApi.sync("KB1", "DS 1", { full: true });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/api/knowledge-bases/KB1/data-sources/DS%201/sync");
    expect(calls[0].init.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ full: true });
  });

  it("sends no body for the incremental sync (with or without options)", async () => {
    const calls = stubFetch();
    await v2KnowledgeApi.sync("KB1", "DS1");
    await v2KnowledgeApi.sync("KB1", "DS1", { full: false });
    expect(calls.map((c) => c.init.body)).toEqual([undefined, undefined]);
  });
});

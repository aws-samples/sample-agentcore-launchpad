// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, RESTART_RETRY_BUDGET_MS, withSignal } from "../lib/api";
import { customEvaluatorRows, resetScaleLabelCatalog } from "../lib/scale-label-catalog";
import { useLoad } from "./hooks";

/**
 * N17-obs476: a page's `useLoad` read is cancelled when the page unmounts or re-reads. Before,
 * a read that failed during a deploy restart kept retrying (N17-obs470) for up to 20 s after
 * the page was gone (obs471 found one). The fetcher runs inside `withSignal(signal, …)`, so
 * every typed-client call it starts carries the page's signal without each of the ~70 api
 * functions taking a parameter; module-level shared caches detach from it.
 */

let fetchMock: ReturnType<typeof vi.fn>;
const down = () => { throw new TypeError("Failed to fetch"); };

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = vi.fn(async () => down());
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  resetScaleLabelCatalog();
});

describe("useLoad read lifetime (N17-obs476)", () => {
  it("unmount stops the restart-window retry and aborts the request", async () => {
    const { unmount } = renderHook(() => useLoad(() => api.listAgents(), "agents"));
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.signal).toBeInstanceOf(AbortSignal);
    expect(init.signal?.aborted).toBe(false);
    unmount();
    expect(init.signal?.aborted).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(RESTART_RETRY_BUDGET_MS + 5_000); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("a new key cancels the read in flight; only the current one retries", async () => {
    const { rerender, unmount } = renderHook(({ k }) => useLoad(() => api.listAgents(), k),
      { initialProps: { k: "a" } });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    rerender({ k: "b" });
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect((fetchMock.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(true);
    await act(async () => { await vi.advanceTimersByTimeAsync(600); });
    expect(fetchMock).toHaveBeenCalledTimes(3); // the current read's first retry only
    unmount();
  });

  it("an aborted read never lands as data or as an error", async () => {
    let answer!: (r: Response) => void;
    fetchMock.mockImplementationOnce(() => new Promise<Response>((resolve) => { answer = resolve; }));
    const { result, unmount } = renderHook(() => useLoad(() => api.listAgents(), "agents"));
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    const last = result.current;
    unmount();
    answer(new Response(JSON.stringify({ agents: [{ id: "late" }] }), { status: 200 }));
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(last.data).toBeNull();
  });

  it("an explicit signal argument wins, and the scope ends with the synchronous call", async () => {
    const ctl = new AbortController();
    withSignal(ctl.signal, () => { void api.listAgents().catch(() => undefined); });
    void api.listAgents().catch(() => undefined); // outside any scope
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect((fetchMock.mock.calls[0][1] as RequestInit).signal).toBe(ctl.signal);
    expect((fetchMock.mock.calls[1][1] as RequestInit).signal ?? null).toBeNull();
  });

  it("a shared module cache is detached: one page unmounting never aborts it for the others", async () => {
    fetchMock.mockImplementation(async () => new Response(JSON.stringify({ evaluators: [
      { id: "cust_1", name: "tone", source: "custom", base_evaluator_id: null, scale_labels: [] }] }),
    { status: 200 }));
    let shared: Promise<unknown> | null = null;
    const { unmount } = renderHook(() => useLoad(() => { shared = customEvaluatorRows(); return shared; }, "x"));
    unmount(); // the page that first asked is gone before the answer
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect((fetchMock.mock.calls[0][1] as RequestInit).signal ?? null).toBeNull();
    await expect(shared).resolves.toHaveProperty("cust_1");
  });
});

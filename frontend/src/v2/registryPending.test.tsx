// @vitest-environment jsdom
import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RESTART_RETRY_BUDGET_MS } from "../lib/api";
import { useRegistryPendingCount } from "./registryPending";

/**
 * N17-obs471: the registry badge's count read is cancelled when the shell unmounts or
 * re-reads — before, a read that failed during a restart kept retrying (N17-obs470) for up
 * to 20 s after its shell was gone; in the test suite that late retry landed in the NEXT
 * test as a "member read the count" (the V2Shell badge flake).
 */

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = vi.fn(async () => { throw new TypeError("Failed to fetch"); });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("registry pending count read lifetime (N17-obs471)", () => {
  it("stops retrying once the shell is gone", async () => {
    const { unmount } = renderHook(() => useRegistryPendingCount(true, "k"));
    await vi.advanceTimersByTimeAsync(10);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    unmount();
    await vi.advanceTimersByTimeAsync(RESTART_RETRY_BUDGET_MS + 5_000);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("a re-read supersedes the one in flight instead of running beside it", async () => {
    const { rerender, unmount } = renderHook(({ k }) => useRegistryPendingCount(true, k), { initialProps: { k: "a" } });
    await vi.advanceTimersByTimeAsync(10);
    rerender({ k: "b" });
    await vi.advanceTimersByTimeAsync(10);
    expect(fetchMock).toHaveBeenCalledTimes(2); // one per key
    await vi.advanceTimersByTimeAsync(600); // only the current read retries
    expect(fetchMock).toHaveBeenCalledTimes(3);
    unmount();
  });
});

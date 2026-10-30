// @vitest-environment jsdom
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HEALTH_DOWN_PROBE_MS, HEALTH_PROBE_MS, useHealth } from "./useHealth";

/** N17-obs470: while the backend restarts (~11 s) the health probe checks again every few
 *  seconds instead of every 30 s, so the shell's 「正在更新……」 clears promptly. */

let fetchMock: ReturnType<typeof vi.fn>;
const up = () => new Response(JSON.stringify({ status: "ok", version: "1", region: "cn-northwest-1" }),
  { status: 200, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("useHealth while the backend is down (N17-obs470)", () => {
  it("re-probes every few seconds while down and back to the steady cadence once up", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(new Response("<html>502</html>", { status: 502 }))
      .mockResolvedValue(up());
    const { result } = renderHook(() => useHealth());
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    expect(result.current.status).toBe("down");
    await act(async () => { await vi.advanceTimersByTimeAsync(HEALTH_DOWN_PROBE_MS); });
    expect(result.current.status).toBe("down");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(HEALTH_DOWN_PROBE_MS); });
    expect(result.current.status).toBe("ok");
    const calls = fetchMock.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(HEALTH_DOWN_PROBE_MS * 3); });
    expect(fetchMock.mock.calls.length).toBe(calls); // steady again: no fast probing
    expect(HEALTH_DOWN_PROBE_MS).toBeLessThan(HEALTH_PROBE_MS);
  });
});

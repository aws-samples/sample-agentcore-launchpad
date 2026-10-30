// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import en from "../locales/en/common.json";
import zhCN from "../locales/zh-CN/common.json";
import { api, ApiError, errorMessage, modelProvidersApi, RESTART_RETRY_BUDGET_MS } from "./api";
import { abandonRestartRetries } from "./restartRetry";

/**
 * N17-obs470 (live 2026-10-26 08:56:23Z): during a deploy the backend is down ~11 s and nginx
 * answers 502 — 准备计划 showed a raw 「Bad Gateway」. A 502 / 503 / 504 without the backend's
 * error envelope (and a network failure) now reads 「控制台正在更新，请稍后几秒再试」; an idempotent
 * GET retries with a short backoff for up to ~20 s; a POST / PUT / DELETE is NEVER retried.
 */

const UPDATING = en.apiErrors["http.502"];

const gateway = (status = 502) =>
  new Response("<html><body><h1>502 Bad Gateway</h1></body></html>",
    { status, statusText: "Bad Gateway", headers: { "Content-Type": "text/html" } });
const ok = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function settle<T>(p: Promise<T>): Promise<{ value?: T; error?: unknown; at?: number }> {
  const out: { value?: T; error?: unknown; at?: number } = {};
  const done = p.then((v) => { out.value = v; out.at = Date.now(); },
    (e) => { out.error = e; out.at = Date.now(); });
  await vi.advanceTimersByTimeAsync(RESTART_RETRY_BUDGET_MS + 5_000);
  await done;
  return out;
}

describe("the restart window (N17-obs470)", () => {
  it("has the friendly copy in both locales for every gateway status and the network", () => {
    for (const s of ["502", "503", "504"]) {
      expect(en.apiErrors[`http.${s}` as "http.502"]).toBe("The console is updating — try again in a few seconds.");
      expect(zhCN.apiErrors[`http.${s}` as "http.502"]).toBe("控制台正在更新，请稍后几秒再试");
    }
    expect(zhCN.apiErrors.network).toContain("请稍后几秒再试");
  });

  it("an idempotent GET rides out the restart: retried with backoff until it answers", async () => {
    fetchMock.mockResolvedValueOnce(gateway()).mockResolvedValueOnce(gateway(504))
      .mockRejectedValueOnce(new TypeError("Failed to fetch")).mockResolvedValueOnce(ok({ agents: [] }));
    const { value, error } = await settle(api.listAgents());
    expect(error).toBeUndefined();
    expect(value).toEqual({ agents: [] });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("a GET that stays down gives up after the budget with the friendly message", async () => {
    fetchMock.mockImplementation(async () => gateway());
    const started = Date.now();
    const { error, at } = await settle(api.listAgents());
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("http.502"); // the code callers key on is kept
    expect((error as ApiError).message).toBe(UPDATING);
    expect(errorMessage(error)).not.toContain("Bad Gateway");
    expect((at ?? 0) - started).toBeLessThanOrEqual(RESTART_RETRY_BUDGET_MS);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(2);
    expect(fetchMock.mock.calls.length).toBeLessThan(15);
  });

  it.each<[string, () => Promise<unknown>]>([
    ["POST", () => modelProvidersApi.create({ name: "x" } as never)],
    ["PUT", () => modelProvidersApi.update("p1", {} as never)],
    ["DELETE", () => modelProvidersApi.remove("p1")],
  ])("a %s is never retried — one call, the friendly message, the caller keeps its form", async (_m, call) => {
    fetchMock.mockImplementation(async () => gateway());
    const { error } = await settle(call());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect((error as ApiError).message).toBe(UPDATING);
  });

  it("a POST that cannot reach the console says so too, without a retry", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const { error } = await settle(modelProvidersApi.create({ name: "x" } as never));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(errorMessage(error)).toBe(en.apiErrors.network);
  });

  it("a backend 503 with its own error envelope is the backend's answer — not retried, not relabelled", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ code: "feature.unavailable", message: "memory is not configured" }),
      { status: 503, headers: { "Content-Type": "application/json" } }));
    const { error } = await settle(api.listAgents());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect((error as ApiError).code).toBe("feature.unavailable");
  });

  it("an aborted GET stops waiting at once", async () => {
    fetchMock.mockImplementation(async () => gateway());
    const ctl = new AbortController();
    const p = api.getAnnouncement("a1", ctl.signal);
    const out: { error?: unknown } = {};
    const done = p.catch((e) => { out.error = e; });
    await vi.advanceTimersByTimeAsync(100);
    ctl.abort();
    await vi.advanceTimersByTimeAsync(100);
    await done;
    expect((out.error as Error).name).toBe("AbortError");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("an abandoned retry never fetches again (N17-obs471: a retry must not outlive its test)", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    const out: { error?: unknown } = {};
    const done = api.listAgents().catch((e) => { out.error = e; });
    await vi.advanceTimersByTimeAsync(10); // the first call failed; a backoff pause is pending
    expect(fetchMock).toHaveBeenCalledTimes(1);
    abandonRestartRetries();
    await vi.advanceTimersByTimeAsync(RESTART_RETRY_BUDGET_MS + 5_000);
    await done;
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect((out.error as Error).name).toBe("AbortError");
  });
});

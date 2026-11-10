// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { ObsTraceDetail } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { tokenSplit } from "../../judgeTokens";
import { TraceDetail } from "./TraceDetail";

/**
 * N17-obs439: the trace's tokens as the backend now reports them for the real Kimi K3 trace of
 * tests/fixtures/obs439_cache_spans.json — 39 uncached input, 1,110 output, 15,919 cache reads,
 * 1,274 cache writes, 18,342 total. The console's "input" is the whole prompt (17,232), the
 * cache split as tooltip — the same meaning SCORE NOW and the run line give it.
 */

const TOKENS = { input: 39, output: 1110, cache_read: 15919, cache_write: 1274, total: 18342 };
const TRACE = {
  trace_id: "6ad8854f68ca803d69edef5350e6a4b6", range: "24h",
  meta: { root_operation: "invoke_agent", service: "paipai", agent: "paipai", session_id: null, start: null,
          duration_ms: 1, span_count: 81, llm_count: 4, status: "ok", tokens: TOKENS },
  tree: [], spans: [], cache: { hit: false, age_s: 0 },
} as unknown as ObsTraceDetail;

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return { ...actual, api: { ...actual.api, obsTrace: () => Promise.resolve(TRACE) } };
});

beforeAll(async () => {
  Element.prototype.scrollIntoView = () => undefined;
  await i18next.use(initReactI18next).init({
    lng: "zh-CN", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zh } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});
afterEach(() => cleanup());

describe("observability token semantics (N17-obs439)", () => {
  it("splits a block with cache counts into its whole prompt and the cached share", () => {
    expect(tokenSplit(TOKENS)).toEqual({ prompt: 17232, uncached: 39, cached: 17193, output: 1110, read: 15919, write: 1274 });
    // a block without cache fields (older backend): the reported input stands
    expect(tokenSplit({ input: 5, output: 3, total: 8 }).prompt).toBe(5);
  });

  it("shows the trace's input as the whole prompt with the cache split as tooltip", async () => {
    render(
      <MemoryRouter>
        <TraceDetail traceId={TRACE.trace_id} range="24h" />
      </MemoryRouter>,
    );
    const line = await screen.findByTestId("v2-obs-tokens-detail");
    expect(line.textContent).toContain("18,342");
    expect(line.textContent).toContain("17,232");
    expect(line.textContent).not.toMatch(/(^|\D)39(\D|$)/);
    expect(line.getAttribute("title")).toBe(
      "输入 17,232 个 token：未缓存 39，缓存读取 15,919，缓存写入 1,274（Bedrock 的 inputTokens 只含未缓存部分）",
    );
  });
});

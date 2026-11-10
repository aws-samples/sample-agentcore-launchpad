// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import { failureDetectionTotals, type InsightTrees } from "../lib/evaluation";
import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { InsightClusters } from "./InsightClusters";

/** N17-obs449: the failure analysis says what it did, so 0 failure clusters can only mean 0
 *  detected — incomplete detections (truncated / unparseable reply), findings the confidence
 *  threshold dropped, or "N analysed, none found"; nothing for a run from an older worker. */

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "zh-CN", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zh } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});
afterEach(async () => {
  cleanup();
  await i18next.changeLanguage("zh-CN");
});

const trees = (...parts: { sessions: number; incomplete: number; lowConfidenceDropped: number }[]): InsightTrees => ({
  failures: [], userIntents: [], executionSummaries: [],
  failureDetection: parts.map((p) => ({ ...p, threshold: "medium" })),
});

describe("failure detection note (N17-obs449)", () => {
  it("sums the worker shards", () => {
    expect(failureDetectionTotals(trees({ sessions: 10, incomplete: 2, lowConfidenceDropped: 1 },
                                        { sessions: 10, incomplete: 1, lowConfidenceDropped: 0 })))
      .toEqual({ sessions: 20, incomplete: 3, lowConfidenceDropped: 1, threshold: "medium" });
    expect(failureDetectionTotals({ failures: [] })).toBeNull();
  });

  it("says how many sessions' detection did not complete and how many low findings were ignored", () => {
    render(<InsightClusters insights={trees({ sessions: 20, incomplete: 20, lowConfidenceDropped: 3 })} />);
    expect(screen.getByTestId("v2-failure-detection-incomplete").textContent).toContain("20 个会话的失败检测未完成（共 20 个）");
    expect(screen.getByTestId("v2-failure-detection-low").textContent).toBe("已忽略 3 条低于置信度阈值（中）的发现");
    expect(screen.queryByTestId("v2-failure-detection-none")).toBeNull();  // not "no failures"
  });

  it("states a clean analysis explicitly, in en too", async () => {
    render(<InsightClusters insights={trees({ sessions: 20, incomplete: 0, lowConfidenceDropped: 0 })} />);
    expect(screen.getByTestId("v2-failure-detection-none").textContent).toBe("已检测 20 个会话，未发现失败");
    cleanup();
    await i18next.changeLanguage("en");
    render(<InsightClusters insights={trees({ sessions: 1, incomplete: 1, lowConfidenceDropped: 1 })} />);
    expect(screen.getByTestId("v2-failure-detection-incomplete").textContent).toContain("did not complete for 1 of 1 sessions");
    expect(screen.getByTestId("v2-failure-detection-low").textContent).toBe("1 finding below the confidence threshold (medium) was ignored");
  });

  it("shows nothing for a run from an older worker (no counts)", () => {
    render(<InsightClusters insights={{ failures: [], userIntents: [], executionSummaries: [] }} />);
    expect(screen.queryByTestId("v2-failure-detection")).toBeNull();
  });
});

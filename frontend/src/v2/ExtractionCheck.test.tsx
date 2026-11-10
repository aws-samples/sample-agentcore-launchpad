// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import type { EvaluationRunResults, ExtractionCheck, ExtractionCheckSummary } from "../lib/evaluation";
import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { ExtractionCheckBanner, ExtractionCheckTag } from "./ExtractionCheck";
import { rowsFromRun } from "./results";
import { taskFromRun } from "./tasks";

/**
 * Eval accuracy item 1: a run whose extraction reconciliation found a mismatch shows a clear
 * error banner (「取数校验未通过：评分可能不可信」) with the per-turn detail on expand, and the
 * mismatching session's score rows are flagged; ok / unverifiable read as such; the anomaly
 * counters ride on every summary.
 */

const anomalies = { turns: 5, empty: 1, tool_json_like: 1, repeated_previous: 1, empty_rate: 0.2 };
const mismatch: ExtractionCheckSummary = {
  status: "mismatch", sessions: 3, ok: 1, mismatch: 1, unverifiable: 1, turns_checked: 3,
  mismatched_sessions: ["sess-b"], anomalies,
};
const sessionCheck: ExtractionCheck = {
  status: "mismatch", source: "replay", turns_checked: 2, mismatch_count: 1,
  mismatches: [{ turn: 1, expected_head: "已查到：450 元/晚", extracted_head: "我先查一下差旅标准" }],
  anomalies: { turns: 2, empty: 0, tool_json_like: 0, repeated_previous: 0 },
};

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "zh-CN", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zh } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});
afterEach(cleanup);

describe("ExtractionCheckBanner", () => {
  it("renders the zh warning with the per-turn detail and the anomaly counters", () => {
    render(<ExtractionCheckBanner summary={mismatch} sessions={[{ session_id: "sess-b", results: [], extraction_check: sessionCheck }]} />);
    const banner = screen.getByTestId("v2-extraction-mismatch");
    expect(banner.textContent).toContain("取数校验未通过：评分可能不可信");
    expect(banner.textContent).toContain("3 个会话中有 1 个");
    const detail = screen.getByTestId("v2-extraction-detail");
    expect(detail.tagName).toBe("DETAILS");
    expect(detail.textContent).toContain("sess-b");
    expect(detail.textContent).toContain("第 1 轮：应为「已查到：450 元/晚」，实际评分用的是「我先查一下差旅标准」");
    expect(screen.getByTestId("v2-extraction-anomalies").textContent).toBe(
      "空回答 1/5（20%）· 疑似工具 JSON 的回答 1 · 与上一轮相同 1",
    );
  });

  it("renders the en warning", async () => {
    await i18next.changeLanguage("en");
    render(<ExtractionCheckBanner summary={mismatch} />);
    expect(screen.getByTestId("v2-extraction-mismatch").textContent).toContain(
      "Extraction check failed: the scores may not be trustworthy",
    );
    await i18next.changeLanguage("zh-CN");
  });

  it("reads ok and unverifiable as such, and nothing without a summary", () => {
    const { rerender, container } = render(<ExtractionCheckBanner summary={{ ...mismatch, status: "ok", ok: 2, mismatch: 0, mismatched_sessions: [] }} />);
    expect(screen.getByTestId("v2-extraction-ok").textContent).toContain("取数校验通过：2 个会话、3 轮");
    expect(screen.getByTestId("v2-extraction-ok").textContent).toContain("另有 1 个会话没有可对照的参考回答");
    rerender(<ExtractionCheckBanner summary={{ ...mismatch, status: "unverifiable", ok: 0, mismatch: 0, unverifiable: 3 }} />);
    expect(screen.getByTestId("v2-extraction-unverifiable").textContent).toContain("取数无法校验");
    rerender(<ExtractionCheckBanner summary={null} />);
    expect(container.textContent).toBe("");
  });

  it("tags one session's check (SCORE NOW) with the mismatch detail on hover", () => {
    render(<ExtractionCheckTag check={sessionCheck} />);
    const tag = screen.getByTestId("v2-extraction-session-mismatch");
    expect(tag.textContent).toBe("取数校验未通过：评分可能不可信");
    expect(tag.getAttribute("title")).toContain("实际评分用的是「我先查一下差旅标准」");
  });
});

describe("mismatching sessions' scores are flagged", () => {
  it("marks every result row of a mismatching session, and only those", () => {
    const run = taskFromRun({
      id: "run-1", agent_id: "a", agent_name: "agent", dataset_name: "ds", mode: "evaluators",
      evaluators: ["Builtin.Helpfulness"], status: "completed", queue_position: 0, scores: [], insights: {},
      session_ids: ["sess-a", "sess-b"], error: null,
    } as never);
    const row = { evaluator_id: "Builtin.Helpfulness", level: "TRACE", score: 1, label: "Very Helpful", explanation: "", error_type: null, error_message: null };
    const results: EvaluationRunResults = {
      run_id: "run-1", batch_eval_id: "run-1", available: true, count: 2, truncated: false,
      sessions: [
        { session_id: "sess-a", results: [row], extraction_check: { ...sessionCheck, status: "ok", mismatches: [], mismatch_count: 0 } },
        { session_id: "sess-b", results: [row], extraction_check: sessionCheck },
      ],
    };
    const rows = rowsFromRun(run, results);
    expect(rows.map((r) => [r.sessionId, r.extractionMismatch])).toEqual([["sess-a", false], ["sess-b", true]]);
  });
});

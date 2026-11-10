// @vitest-environment jsdom
import i18next from "i18next";
import { beforeAll, describe, expect, it } from "vitest";

import type { ScorecardRun } from "../lib/api";
import en from "../locales/en/common.json";
import zhCN from "../locales/zh-CN/common.json";
import { failureHint } from "./pages/tasks/scorecardAttention";
import { RUN_FAILURE_CODES, runFailureText } from "./runFailure";

/** N17-obs450: the 需要关注 panel, the task detail and the run tables showed a run's `error`
 *  in English ("job 9e80f30c69c5 lost after attempt 3 (evaluating 3/10)"); the payload now
 *  names the reason (`error_code` + `error_params`) and the console words it in both
 *  languages, the raw text kept for the tooltip. */

beforeAll(async () => {
  await i18next.init({
    lng: "zh-CN", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zhCN } },
    defaultNS: "common", interpolation: { escapeValue: false },
  });
});

const zh = () => i18next.getFixedT("zh-CN");
const enT = () => i18next.getFixedT("en");
const PARAMS = { job_id: "j1", attempt: 3, progress: "evaluating 3/10", minutes: 30, total: 36, count: 26,
  unit: "evaluator_records", first_error: "E: m", first_class: "failed_turn", code: "run.no_sessions",
  message: "none", type: "TimeoutError", failure_class: "timeout" };

describe("run failure copy (N17-obs450)", () => {
  it("words every code in both languages", () => {
    for (const code of RUN_FAILURE_CODES) {
      for (const t of [enT(), zh()]) {
        const text = runFailureText({ error: "raw", error_code: code, error_params: PARAMS }, t);
        expect(text, code).toBeTruthy();
        expect(text, code).not.toMatch(/runFailure|unitCoverage|\{\{/);
      }
    }
  });

  it("reads the live examples in Chinese", () => {
    expect(runFailureText({ error: "job 9e80f30c69c5 lost after attempt 3 (evaluating 3/10)", error_code: "job_lost",
      error_params: { job_id: "9e80f30c69c5", attempt: 3, progress: "evaluating 3/10" } }, zh()))
      .toBe("评估任务 9e80f30c69c5 在第 3 次尝试后失联（evaluating 3/10）");
    expect(runFailureText({ error: "stopped by operator", error_code: "stopped_by_operator", error_params: {} }, zh()))
      .toBe("已被操作员停止");
    expect(runFailureText({ error: "26 of 36 …", error_code: "some_records_errored", error_params: { count: 26,
      total: 36, unit: "evaluator_records", first_error: "span_status_error: turn t not scored",
      first_class: "failed_turn" } }, zh()))
      .toBe("26/36 条评估器记录出错 · 首个错误（失败的轮次）：span_status_error: turn t not scored");
  });

  it("falls back to the raw text on an older payload, an unknown code or no known shape", () => {
    expect(runFailureText({ error: "old text" }, zh())).toBe("old text");
    expect(runFailureText({ error: "x", error_code: "brand_new", error_params: {} }, zh())).toBe("x");
    expect(runFailureText({ error: "x", error_code: null }, zh())).toBe("x");
    expect(runFailureText({ error: null }, zh())).toBeNull();
    expect(runFailureText(undefined, zh())).toBeNull();
  });

  it("gives the attention panel the worded, clipped first line", () => {
    const run = { error: "job 9e80f30c69c5 lost after attempt 3 (evaluating 3/10)", error_code: "job_lost",
      error_params: { job_id: "9e80f30c69c5", attempt: 3, progress: "evaluating 3/10" } } as unknown as ScorecardRun;
    expect(failureHint(run, zh())).toBe("评估任务 9e80f30c69c5 在第 3 次尝试后失联（evaluating 3/10）");
    expect(failureHint({ error: null } as unknown as ScorecardRun, zh())).toBe("");
  });
});

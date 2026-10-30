// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { ObsSessionScore, ObsSessionScoreResult } from "../../../lib/api";
import type { ExtractionCheck } from "../../../lib/evaluation";
import { resetScaleLabelCatalog } from "../../../lib/scale-label-catalog";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { ScoreNow } from "./SessionDetail";

/**
 * N17-obs436 (EV-02 ch07): SCORE NOW names each evaluator as the online-scores card does — a
 * custom evaluator by its catalogue name, a built-in by its localized name, the id as tooltip
 * — never the server's bare `evaluator_name` (cust_… / "Correctness"). Its token cell shows
 * the judge's whole prompt: Bedrock's `inputTokens` excludes prompt-cache reads (live: 2 next to
 * 1,251 output) while `totalTokens` counts them.
 */

const { CUSTOM, row, scoreResponse } = vi.hoisted(() => ({
  CUSTOM: "cust_fca995cdc705",
  scoreResponse: { current: null as ObsSessionScore | null },
  row: (evaluator_id: string, token_usage: ObsSessionScoreResult["token_usage"]): ObsSessionScoreResult => ({
    evaluator_id,
    evaluator_name: evaluator_id.startsWith("Builtin.") ? evaluator_id.split(".")[1] : evaluator_id,
    evaluator_arn: `arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:evaluator/${evaluator_id}`,
    value: 1, label: null, explanation: "ok", span_context: null, token_usage,
    error_code: null, error_message: null,
  }) as ObsSessionScoreResult,
}));

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  const evaluators = [
    { id: "Builtin.Correctness", level: "TRACE", source: "builtin" },
    { id: CUSTOM, name: "hr_next_step_check", level: "TRACE", source: "custom", definition: "judge", scale_labels: [] },
  ];
  return {
    ...actual,
    api: {
      ...actual.api,
      v2Evaluators: () => Promise.resolve({ evaluators, builtin_count: 1 }),
      obsEvaluateSession: () => Promise.resolve(scoreResponse.current ?? {
        session_id: "s".repeat(64), range: "24h", span_count: 31, extraction_check: null,
        results: [
          row(CUSTOM, { input: 1372, output: 380, total: 1752 }),
          // the live Correctness row: 2 uncached input tokens, the cached prompt only in total
          row("Builtin.Correctness", { input: 2, output: 1251, total: 7253 }),
        ],
      }),
    },
  };
});

beforeAll(async () => {
  // jsdom has no layout: the panel scrolls its outcome into view once it lands
  Element.prototype.scrollIntoView = () => undefined;
  await i18next.use(initReactI18next).init({
    lng: "zh-CN", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zh } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});
afterEach(() => {
  cleanup();
  resetScaleLabelCatalog();
  scoreResponse.current = null;
});

async function scored() {
  render(
    <MemoryRouter>
      <ScoreNow sessionId={"s".repeat(64)} range="24h" />
    </MemoryRouter>,
  );
  fireEvent.click(await screen.findByTestId("v2-obs-score-Builtin.Correctness"));
  fireEvent.click(screen.getByTestId("v2-obs-score-run"));
  fireEvent.click(await screen.findByTestId("v2-confirm-ok"));
  return within(await screen.findByTestId("v2-obs-score-results"));
}

describe("SCORE NOW results (N17-obs436)", () => {
  it("names custom evaluators by their catalogue name and built-ins in zh, the id as tooltip", async () => {
    const results = await scored();
    await waitFor(() => expect(results.getByText("hr_next_step_check")).toBeTruthy());
    expect(results.getByText("hr_next_step_check").closest("[title]")?.getAttribute("title")).toBe(CUSTOM);
    expect(results.getByText("正确性").closest("[title]")?.getAttribute("title")).toBe("Builtin.Correctness");
    expect(results.queryByText("Correctness")).toBeNull();
  });

  it("shows the judge's whole prompt as input, the cached share as tooltip", async () => {
    const results = await scored();
    const cells = await results.findAllByTestId("v2-score-tokens");
    // an uncached call reads as before
    expect(cells[0].textContent).toBe("1,372 / 380");
    expect(cells[0].getAttribute("title")).toBeNull();
    // 7,253 total − 1,251 output = 6,002 prompt tokens, 6,000 of them cache reads
    expect(cells[1].textContent).toBe("6,002 / 1,251");
    expect(cells[1].getAttribute("title")).toBe(
      "输入 6,002 个 token，其中 6,000 个来自提示缓存（Bedrock 只把未缓存的 2 个计为 inputTokens）",
    );
  });

  it("keeps older responses without mapped sources unchanged", async () => {
    await scored();
    expect(screen.queryByTestId("v2-score-sources")).toBeNull();
    expect(screen.queryByTestId("v2-score-result-source")).toBeNull();
  });

  it("labels a mapped single-source result with its runtime ID and check", async () => {
    scoreResponse.current = mappedScore(["lpp-runtime-one"]);
    const results = await scored();
    expect(results.getByTestId("v2-score-result-source").textContent).toContain("lpp-runtime-one");
    expect(results.queryByText("s".repeat(64))).toBeNull();
    const check = within(screen.getByTestId("v2-score-source-check"));
    expect(check.getByText("lpp-runtime-one")).toBeTruthy();
    expect(check.getByTestId("v2-extraction-session-ok")).toBeTruthy();
    expect(screen.queryByText(zh.v2.observability.score.separateSources)).toBeNull();
  });

  it("keeps each runtime's score and extraction check attributable without combining them", async () => {
    scoreResponse.current = mappedScore(["lpp-runtime-one", "lpp-runtime-two"]);
    scoreResponse.current.extraction_checks!["lpp-runtime-two"] = {
      ...okCheck, status: "mismatch", mismatch_count: 1,
      mismatches: [{ turn: 1, expected_head: "Expected answer", extracted_head: "Different answer" }],
    };
    scoreResponse.current.results[1].value = 0;
    const results = await scored();
    expect(screen.getByText(zh.v2.observability.score.separateSources)).toBeTruthy();
    for (const [index, source] of ["lpp-runtime-one", "lpp-runtime-two"].entries()) {
      const sourceCell = results.getAllByTestId("v2-score-result-source")[index];
      expect(sourceCell.textContent).toContain(source);
      expect(sourceCell.closest("tr")?.textContent).toContain(index === 0 ? "1.00" : "0.00");
    }
    const checks = screen.getAllByTestId("v2-score-source-check");
    expect(within(checks[0]).getByTestId("v2-extraction-session-ok")).toBeTruthy();
    expect(within(checks[1]).getByTestId("v2-extraction-session-mismatch")).toBeTruthy();
    expect(screen.getByTestId("v2-obs-score-extraction-mismatch")).toBeTruthy();
  });

  it("does not invent a successful check or display sources outside the returned list", async () => {
    scoreResponse.current = mappedScore(["lpp-runtime-one", "lpp-runtime-two"]);
    scoreResponse.current.extraction_checks!["lpp-runtime-two"] = null;
    scoreResponse.current.extraction_checks!["unlisted-runtime"] = okCheck;
    await scored();
    const checks = screen.getAllByTestId("v2-score-source-check");
    expect(within(checks[1]).getByText(zh.v2.observability.score.checkUnavailable)).toBeTruthy();
    expect(within(checks[1]).queryByTestId("v2-extraction-session-ok")).toBeNull();
    expect(screen.queryByText("unlisted-runtime")).toBeNull();
  });
});

const okCheck: ExtractionCheck = {
  status: "ok", source: "runtime_logs", turns_checked: 2, mismatch_count: 0, mismatches: [],
  anomalies: { turns: 2, empty: 0, tool_json_like: 0, repeated_previous: 0 },
};

function mappedScore(sources: string[]): ObsSessionScore {
  return {
    session_id: "s".repeat(64), range: "24h", span_count: 31,
    source_session_ids: sources,
    extraction_checks: Object.fromEntries(sources.map((id) => [id, okCheck])),
    extraction_check: sources.length === 1 ? okCheck : null,
    results: sources.map((source) => ({
      ...row("Builtin.Correctness", null),
      span_context: { sessionId: source, traceId: "a".repeat(32) },
    })),
  };
}

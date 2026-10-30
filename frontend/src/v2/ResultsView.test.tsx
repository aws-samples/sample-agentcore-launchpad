// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { ResultsTable } from "./ResultsView";
import type { ResultRow } from "./results";

/**
 * N17-obs433: the back-flow button counts distinct failed SESSIONS, not failed results —
 * its label names the unit, its tooltip says one dataset case per session (≤ 50, the table's
 * order), and the dialog it opens counts the same sessions.
 */

vi.mock("../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      v2Evaluators: () => Promise.resolve({ evaluators: [], builtin_count: 0 }),
      v2Datasets: () => Promise.resolve({ datasets: [] }),
    },
  };
});

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

function row(i: number, sessionId: string, outcome: ResultRow["outcome"], evaluatorId = "Builtin.Correctness"): ResultRow {
  return {
    key: `r${i}`, time: "2026-10-25T06:56:49Z", taskKey: "run:1", taskName: "t", agent: "hr-eval-demo",
    agentId: "a1", source: "dataset", sessionId, traceId: null, evaluatorId, level: "TRACE",
    score: outcome === "failed" ? 0 : 1, normalized: outcome === "failed" ? 0 : 1, label: null,
    explanation: null, error: null, outcome,
  };
}

// 11 failed results over 4 sessions (two evaluators fail the same sessions), 1 passed
const rows: ResultRow[] = [
  ...["s1", "s2", "s3", "s4", "s1", "s2", "s3", "s4", "s1", "s2", "s3"].map((s, i) =>
    row(i, s, "failed", i < 4 ? "Builtin.Correctness" : i < 8 ? "Builtin.Helpfulness" : "Builtin.Faithfulness")),
  row(99, "s5", "passed"),
];

function mount(list: ResultRow[] = rows) {
  return render(
    <MemoryRouter>
      <ResultsTable rows={list} range="24h" exportName="x" />
    </MemoryRouter>,
  );
}

describe("ResultsTable back-flow button (N17-obs433)", () => {
  it("counts sessions, names the unit and explains it in the tooltip (zh)", async () => {
    mount();
    const button = screen.getByTestId("v2-results-badcases");
    expect(button.textContent).toBe("不通过会话回流数据集（4 个会话）");
    expect(button.getAttribute("title")).toContain("每个含不通过结果的会话最多生成一条数据集用例");
    expect(button.getAttribute("title")).toContain("最多取前 50 个会话");
    // the dialog counts the same sessions
    fireEvent.click(button);
    await waitFor(() => expect(document.body.textContent).toContain("将把 4 个会话转换为数据集用例"));
  });

  it("uses the singular and plural session unit in en", async () => {
    await i18next.changeLanguage("en");
    mount([row(1, "s1", "failed"), row(2, "s1", "failed", "Builtin.Helpfulness")]);
    expect(screen.getByTestId("v2-results-badcases").textContent).toBe("Add 1 failed session to a dataset");
    cleanup();
    mount();
    const button = screen.getByTestId("v2-results-badcases");
    expect(button.textContent).toBe("Add 4 failed sessions to a dataset");
    expect(button.getAttribute("title")).toContain("Counts sessions, not results");
  });

  it("caps at 50 sessions", () => {
    mount(Array.from({ length: 60 }, (_, i) => row(i, `sess-${i}`, "failed")));
    expect(screen.getByTestId("v2-results-badcases").textContent).toBe("不通过会话回流数据集（50 个会话）");
  });
});

// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import {
  planRecommendationGate,
  recommendationGate,
  recommendationText,
  unresolvedBlockingCount,
} from "../lib/evalPlanGate";
import { EvalGateBadge } from "./EvalGateBadge";

/**
 * WB-006: a plan entry (or recommendation) that blocks publishing is never rendered as
 * "informational" — it reads "blocking · threshold N" (zh "阻断 · 阈值 N"); the plan
 * card counts unresolved blocking recommendations; proposal entries may be objects.
 */

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en }, "zh-CN": { common: zh } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

afterEach(async () => {
  cleanup();
  await i18next.changeLanguage("en");
});

const text = (id = "eval-gate") => screen.getByTestId(id).textContent ?? "";

describe("EvalGateBadge (WB-006)", () => {
  it("renders a blocking entry with its threshold and never as informational (en)", () => {
    render(<EvalGateBadge blocking threshold={1} />);
    expect(text()).toBe("blocking · threshold 1.00");
    expect(text()).not.toContain("informational");
    expect(screen.getByTestId("eval-gate").getAttribute("data-blocking")).toBe("true");
  });

  it("renders a blocking entry in zh-CN with full-width wording", async () => {
    await i18next.changeLanguage("zh-CN");
    render(<EvalGateBadge blocking threshold={0.9} metric="通过率" source="parsed" />);
    expect(text()).toBe("阻断 · 阈值 0.90 · 指标 通过率由建议文本解析");
    expect(text()).not.toContain("参考");
  });

  it("renders blocking without a threshold, and informational with a display threshold", () => {
    render(<EvalGateBadge blocking threshold={null} testId="a" />);
    render(<EvalGateBadge blocking={false} threshold={0.7} testId="b" />);
    render(<EvalGateBadge blocking={false} threshold={null} testId="c" />);
    expect(text("a")).toBe("blocking");
    expect(text("b")).toBe("informational · ≥ 0.70");
    expect(text("c")).toBe("informational");
  });

  it("labels where the gate came from", () => {
    render(<EvalGateBadge blocking threshold={1} source="stated" testId="s" />);
    expect(text("s-source")).toBe("stated in the proposal");
    render(<EvalGateBadge blocking threshold={1} source="parsed" testId="p" />);
    expect(text("p-source")).toBe("parsed from the recommendation text");
  });
});

describe("recommendation helpers", () => {
  it("reads the text and gate of string and object proposal entries", () => {
    expect(recommendationText("Builtin.Helpfulness")).toBe("Builtin.Helpfulness");
    const entry = { text: "guardrails gate", evaluator_id: "hr-guardrails-judge", blocking: true, threshold: 1 };
    expect(recommendationText(entry)).toBe("guardrails gate");
    expect(recommendationGate(entry)).toEqual({ blocking: true, threshold: 1, metric: null, source: "stated" });
    expect(recommendationGate("blocking at threshold 1.0")).toBeNull(); // the console never parses prose
    expect(recommendationGate({ text: "no gate stated" })).toBeNull();
  });

  it("counts unresolved recommendations that ask for a gate, and only those", () => {
    const rows = [
      { index: 0, status: "unresolved", blocking: true, threshold: 1, intent_source: "parsed" },
      { index: 1, status: "unresolved", threshold: 0.8 },
      { index: 2, status: "unresolved" },
      { index: 3, status: "mapped", blocking: true, threshold: 1 },
      { index: 4, status: "declined", blocking: true },
    ];
    expect(unresolvedBlockingCount(rows)).toBe(2);
    expect(planRecommendationGate(rows[0])).toEqual({ blocking: true, threshold: 1, metric: null, source: "parsed" });
    expect(planRecommendationGate(rows[2])).toBeNull();
  });
});

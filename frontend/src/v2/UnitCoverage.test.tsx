// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import type { UnitCoverage } from "../lib/evaluation";
import en from "../locales/en/common.json";
import { UnitCoverageBanner } from "./UnitCoverage";

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});
afterEach(() => cleanup());

const coverage = (over: Partial<UnitCoverage>): UnitCoverage => ({
  expected: 9, judged: 5, missing: 1, coverage: 0.5556, threshold: 0.9, below_threshold: true,
  failures: { timeout: 1, throttling: 1, truncation: 1 }, evaluators: [], ...over,
});

describe("UnitCoverageBanner (R3-f)", () => {
  it("warns below the threshold with the causes and the missing units", () => {
    render(<UnitCoverageBanner coverage={coverage({})} />);
    expect(screen.getByTestId("v2-unit-coverage").textContent).toContain("Scored 5 of 9 expected units (56%)");
    expect(screen.getByTestId("v2-unit-coverage-below").textContent).toContain("90%");
    const text = screen.getByTestId("v2-unit-coverage").textContent ?? "";
    expect(text).toContain("timeout 1");
    expect(text).toContain("throttling 1");
    expect(text).toContain("truncated 1");
    expect(text).toContain("1 units produced no result at all");
  });

  it("states a full run without the warning, and nothing for a run without the block", () => {
    render(<UnitCoverageBanner coverage={coverage({ judged: 9, missing: 0, coverage: 1, below_threshold: false, failures: {} })} />);
    expect(screen.getByTestId("v2-unit-coverage").textContent).toContain("Scored 9 of 9");
    expect(screen.queryByTestId("v2-unit-coverage-below")).toBeNull();
    cleanup();
    const { container } = render(<UnitCoverageBanner coverage={null} />);
    expect(container.textContent).toBe("");
  });
});

describe("run token usage (P8.3 / P8.4)", () => {
  it("shows the run's judge tokens and a spent budget", () => {
    render(<UnitCoverageBanner coverage={coverage({})} tokens={{ inputTokens: 120, outputTokens: 30, totalTokens: 150, budget: 100, exhausted: true }} />);
    const text = screen.getByTestId("v2-run-tokens").textContent ?? "";
    expect(text).toContain("120 in / 30 out (150 total)");
    expect(text).toContain("token budget (100) ran out");
    expect(screen.getByTestId("v2-run-tokens").getAttribute("title")).toBeNull();  // nothing cached
  });

  it("counts the judges' cache reads as input, as SCORE NOW does (N17-obs436)", () => {
    // Bedrock: inputTokens excludes cache reads, totalTokens = input + output + cache reads
    render(<UnitCoverageBanner coverage={coverage({})} tokens={{ inputTokens: 40, outputTokens: 2500, totalTokens: 12540, budget: null, exhausted: false }} />);
    const line = screen.getByTestId("v2-run-tokens");
    expect(line.textContent).toContain("10,040 in / 2,500 out (12,540 total)");
    expect(line.getAttribute("title")).toBe(
      "10,040 input tokens, 10,000 of them from the prompt cache (Bedrock counts only the 40 uncached ones as inputTokens)");
  });
});

describe("agent failures (N17-obs414)", () => {
  it("reports failed agent turns apart from the coverage", () => {
    render(<UnitCoverageBanner coverage={coverage({ expected: 57, judged: 57, coverage: 1, below_threshold: false, missing: 0, failures: {}, agent_failures: 7 })} />);
    expect(screen.queryByTestId("v2-unit-coverage-below")).toBeNull();
    expect(screen.getByTestId("v2-unit-coverage-agent-failures").textContent).toContain("7 agent turns failed");
  });
});

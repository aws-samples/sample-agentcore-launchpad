// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import type { RunCoverage } from "../lib/evaluation";
import { RunCoverageNote } from "./RunCoverage";

/** N12-obs62: the task detail's unscored-scenario list names the experiment / canary arm of
 *  every scenario the hub attributed one to, so a canary's failures can be read per arm. */

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

afterEach(cleanup);

const coverage = (arms: Record<number, string>): RunCoverage => ({
  total: 9, scored: 7, failed: 2, kind: "replay_failed", excluded: [3, 7], arms,
  firstError: "RuntimeError: scenario s3 failed after 3/3 attempts: RuntimeError: boom",
  legacy: false, records: null,
});

describe("RunCoverageNote", () => {
  it("names the arm of each unscored scenario that has one", () => {
    render(<RunCoverageNote coverage={coverage({ 3: "T1", 7: "C" })} rawError={null} />);
    expect(screen.getByTestId("v2-run-coverage-scenarios").textContent).toBe("Unscored scenarios: #3 (T1), #7 (C)");
  });

  it("lists bare positions on a plain run", () => {
    render(<RunCoverageNote coverage={coverage({})} rawError={null} />);
    expect(screen.getByTestId("v2-run-coverage-scenarios").textContent).toBe("Unscored scenarios: #3, #7");
  });
});

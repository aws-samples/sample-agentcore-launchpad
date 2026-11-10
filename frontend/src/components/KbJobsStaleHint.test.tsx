// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { KbJobsStaleHint } from "./KbJobsStaleHint";

/**
 * r7-j: `jobs_degraded` (N-SCN-obs11) had no rendering; the hint shows only while the
 * flag is true and carries the real en / zh-CN copy.
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

afterEach(cleanup);

describe("KbJobsStaleHint (N-SCN-obs11 / r7-j)", () => {
  it("renders the hint with its tooltip while jobs_degraded is true", () => {
    render(<KbJobsStaleHint degraded className="sub" />);
    const hint = screen.getByTestId("kb-jobs-stale");
    expect(hint.textContent).toBe("Job list may be stale");
    expect(hint.getAttribute("title")).toBe(en.knowledge.detail.sources.jobsStaleTitle);
    expect(hint.className).toBe("sub");
  });

  it("renders nothing when the flag is false or absent", () => {
    const { container, rerender } = render(<KbJobsStaleHint degraded={false} />);
    expect(container.innerHTML).toBe("");
    rerender(<KbJobsStaleHint />);
    expect(container.innerHTML).toBe("");
    expect(screen.queryByTestId("kb-jobs-stale")).toBeNull();
  });

  it("uses the zh-CN copy under zh-CN", async () => {
    await i18next.changeLanguage("zh-CN");
    try {
      render(<KbJobsStaleHint degraded />);
      expect(screen.getByTestId("kb-jobs-stale").textContent).toBe("任务列表可能滞后");
    } finally {
      await i18next.changeLanguage("en");
    }
  });
});

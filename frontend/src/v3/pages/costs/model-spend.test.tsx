import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { customRangeError, periodLabel } from "./costPeriod";
import { ModelSpendPanel } from "./ModelSpend";

describe("cost by model and period", () => {
  beforeAll(async () => {
    vi.stubGlobal("document", { documentElement: {} });
    await i18n.changeLanguage("en");
  });

  it("lists each model's dollars and share, and marks an unpriced one", () => {
    const html = renderToStaticMarkup(<ModelSpendPanel total={20} rows={[
      { model: "global.anthropic.claude-sonnet-5-5", agents: 2, llm_calls: 40, tokens_in: 1_000_000,
        tokens_out: 100_000, cache_tokens: 0, tokens: 1_100_000, est_cost_usd: 15 },
      { model: "global.moonshotai.kimi-k3", agents: 1, llm_calls: 3, tokens_in: 900, tokens_out: 100,
        cache_tokens: 0, tokens: 1000, est_cost_usd: null },
    ]} />);
    expect(html).toContain("By model");
    expect(html).toContain("global.anthropic.claude-sonnet-5-5");
    expect(html).toContain("$15.00");
    expect(html).toContain("75.0%");
    expect(html).toContain("no price");
    expect(html).toContain("1,000,000");
  });

  it("validates a custom range like the backend", () => {
    const now = new Date("2026-11-05T10:00:00Z");
    expect(customRangeError("2026-10-01", "2026-10-31", now)).toBeNull();
    expect(customRangeError("2026-10-31", "2026-10-01", now)).toBe("order");
    expect(customRangeError("2026-12-01", "2026-12-02", now)).toBe("future");
    expect(customRangeError("2026-01-01", "2026-11-01", now)).toBe("tooLong");
    expect(customRangeError("", "2026-11-01", now)).toBe("missing");
  });

  it("labels a period with its last whole day", () => {
    expect(periodLabel({ start: "2026-10-26T00:00:00+00:00", end: "2026-11-02T00:00:00+00:00" }))
      .toBe("2026-10-26 – 2026-11-01");
    expect(periodLabel({ start: "2026-11-05T00:00:00+00:00", end: "2026-11-05T10:30:00+00:00" }))
      .toBe("2026-11-05");
  });
});

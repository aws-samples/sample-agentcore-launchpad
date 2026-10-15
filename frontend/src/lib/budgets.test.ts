import { describe, expect, it } from "vitest";

import type { MyUsage } from "./api";
import {
  BANNER_PERCENT,
  BUDGET_EXCEEDED,
  BUDGET_MODEL_UNPRICED,
  bannerDismissKey,
  bannerLevel,
  barPercent,
  basisKey,
  budgetRefusalOf,
  budgetRefusalText,
  currentPeriod,
  fmtPercent,
  fmtRate,
  fmtResetDate,
  fmtShare,
  fmtUsd,
  isBudgetCode,
  isExemptBasis,
  isPeriod,
  modelPricePath,
  recentPeriods,
  sourceKey,
  usageTone,
} from "./budgets";

function usage(over: Partial<MyUsage> = {}): MyUsage {
  return {
    user_id: "u1",
    username: "alice",
    level: { id: "l1", name: "Standard" },
    basis: "level",
    limit_usd: 10,
    used_usd: 2,
    remaining_usd: 8,
    estimated_usd: 0,
    estimated_share: 0,
    percent_used: 20,
    unlimited: false,
    period: "2026-10",
    resets_at: "2026-11-01T00:00:00+00:00",
    by_source: [],
    ...over,
  };
}

/** A fake `t`: the key, then its options as `k=v` pairs, so tests read what was filled. */
const t = (key: string, options?: Record<string, unknown>) =>
  options ? `${key}|${Object.entries(options).map(([k, v]) => `${k}=${String(v)}`).join(",")}` : key;

describe("refusal codes", () => {
  it("knows exactly the two 402 codes", () => {
    expect(isBudgetCode(BUDGET_EXCEEDED)).toBe(true);
    expect(isBudgetCode(BUDGET_MODEL_UNPRICED)).toBe(true);
    expect(isBudgetCode("aws.throttled")).toBe(false);
    expect(isBudgetCode(undefined)).toBe(false);
  });

  it("reads the code off an error by shape", () => {
    expect(budgetRefusalOf({ code: "budget.exceeded", message: "x" })).toBe("budget.exceeded");
    expect(budgetRefusalOf({ code: "budget.model_unpriced" })).toBe("budget.model_unpriced");
    expect(budgetRefusalOf({ code: "http.402" })).toBeNull();
    expect(budgetRefusalOf(new Error("boom"))).toBeNull();
    expect(budgetRefusalOf(null)).toBeNull();
    expect(budgetRefusalOf("budget.exceeded")).toBeNull();
  });
});

describe("budgetRefusalText", () => {
  it("fills the amounts and the reset date of budget.exceeded", () => {
    const text = budgetRefusalText(
      "budget.exceeded",
      { used_usd: 10.0412, limit_usd: 10, period: "2026-10", resets_at: "2026-11-01T00:00:00+00:00", level: "Standard" },
      t,
    );
    expect(text).toBe("budgetRefusal.exceeded|used=$10.04,limit=$10.00,date=2026-11-01");
  });

  it("falls back to a plain sentence when the detail is missing or partial", () => {
    expect(budgetRefusalText("budget.exceeded", null, t)).toBe("budgetRefusal.exceededPlain");
    expect(budgetRefusalText("budget.exceeded", { used_usd: 1 }, t)).toBe("budgetRefusal.exceededPlain");
    expect(budgetRefusalText("budget.exceeded", { used_usd: "1", limit_usd: 2, resets_at: "x" }, t)).toBe(
      "budgetRefusal.exceededPlain",
    );
  });

  it("names the unpriced model, or says the model is unknown", () => {
    expect(budgetRefusalText("budget.model_unpriced", { model_id: "qwen.qwen3-4b" }, t)).toBe(
      "budgetRefusal.modelUnpriced|model=qwen.qwen3-4b",
    );
    expect(budgetRefusalText("budget.model_unpriced", { model_id: null }, t)).toBe("budgetRefusal.modelUnpricedUnknown");
    expect(budgetRefusalText("budget.model_unpriced", undefined, t)).toBe("budgetRefusal.modelUnpricedUnknown");
  });

  it("leaves every other code to the generic localizer", () => {
    expect(budgetRefusalText("budget.level_in_use", { users: 2 }, t)).toBeNull();
    expect(budgetRefusalText("http.402", {}, t)).toBeNull();
  });
});

describe("formatting", () => {
  it("formats dollars, never $0 for a missing amount", () => {
    expect(fmtUsd(null)).toBe("—");
    expect(fmtUsd(Number.NaN)).toBe("—");
    expect(fmtUsd(0)).toBe("$0.00");
    expect(fmtUsd(0.0042)).toBe("$0.0042");
    expect(fmtUsd(1234.5)).toBe("$1,234.50");
  });

  it("formats unit rates far below a cent without an exponent", () => {
    expect(fmtRate(0.000005)).toBe("$0.000005");
    expect(fmtRate(0.0000005)).toBe("$0.0000005");
    expect(fmtRate(0.0895)).toBe("$0.0895");
    expect(fmtRate(3)).toBe("$3");
    expect(fmtRate(10000, false)).toBe("10,000");
    expect(fmtRate(null)).toBe("—");
  });

  it("formats percentages and shares", () => {
    expect(fmtPercent(82.456)).toBe("82.5%");
    expect(fmtPercent(100)).toBe("100%");
    expect(fmtPercent(null)).toBe("—");
    expect(fmtShare(0.25)).toBe("25%");
    expect(fmtShare(0.12345)).toBe("12.3%");
  });

  it("shows the reset instant as its UTC date", () => {
    expect(fmtResetDate("2026-11-01T00:00:00+00:00")).toBe("2026-11-01");
    expect(fmtResetDate("2027-01-01T00:00:00Z")).toBe("2027-01-01");
    expect(fmtResetDate("not a date")).toBe("not a date");
    expect(fmtResetDate(null)).toBe("—");
  });
});

describe("periods", () => {
  it("validates YYYY-MM", () => {
    expect(isPeriod("2026-10")).toBe(true);
    expect(isPeriod("2026-13")).toBe(false);
    expect(isPeriod("2026-1")).toBe(false);
    expect(isPeriod("")).toBe(false);
    expect(isPeriod(null)).toBe(false);
  });

  it("uses the UTC month", () => {
    // 23:30 on Oct 31 in UTC-5 is already November in UTC
    expect(currentPeriod(new Date("2026-11-01T04:30:00Z"))).toBe("2026-11");
    expect(currentPeriod(new Date("2026-10-31T23:59:59Z"))).toBe("2026-10");
  });

  it("lists recent periods newest first across a year boundary", () => {
    expect(recentPeriods(new Date("2026-02-10T00:00:00Z"), 4)).toEqual(["2026-02", "2026-01", "2025-12", "2025-11"]);
    expect(recentPeriods(new Date("2026-10-15T00:00:00Z"))).toHaveLength(12);
  });
});

describe("limits and the banner", () => {
  it("maps a basis to its copy key and tells the exempt ones apart", () => {
    expect(basisKey("default_level")).toBe("v2.usage.basis.default_level");
    expect(isExemptBasis("unlimited")).toBe(true);
    expect(isExemptBasis("exempt_admin")).toBe(true);
    expect(isExemptBasis("exempt_principal")).toBe(true);
    expect(isExemptBasis("override")).toBe(false);
    expect(isExemptBasis("level")).toBe(false);
    expect(sourceKey("skill_lab")).toBe("v2.usage.source.skill_lab");
  });

  it("is quiet under 80 % and for an unlimited person", () => {
    expect(BANNER_PERCENT).toBe(80);
    expect(bannerLevel(usage({ percent_used: 79.99, used_usd: 7.999 }))).toBeNull();
    expect(
      bannerLevel(usage({ unlimited: true, limit_usd: null, percent_used: null, used_usd: 500, basis: "exempt_admin" })),
    ).toBeNull();
  });

  it("warns from 80 % and reports reached at the limit, as the gate refuses", () => {
    expect(bannerLevel(usage({ percent_used: 80, used_usd: 8 }))).toBe("warn");
    expect(usageTone(usage({ percent_used: 95, used_usd: 9.5 }))).toBe("warn");
    // the gate refuses at used >= limit, whatever the rounded percentage says
    expect(bannerLevel(usage({ percent_used: 100, used_usd: 10 }))).toBe("exceeded");
    expect(bannerLevel(usage({ percent_used: 130, used_usd: 13 }))).toBe("exceeded");
    // a zero budget is reached from the first cent (and at $0)
    expect(bannerLevel(usage({ limit_usd: 0, used_usd: 0, percent_used: 100 }))).toBe("exceeded");
  });

  it("dismisses per period and level", () => {
    expect(bannerDismissKey("2026-10", "warn")).toBe("2026-10:warn");
    expect(bannerDismissKey("2026-10", "warn")).not.toBe(bannerDismissKey("2026-10", "exceeded"));
    expect(bannerDismissKey("2026-10", "warn")).not.toBe(bannerDismissKey("2026-11", "warn"));
  });

  it("caps the bar at 0–100", () => {
    expect(barPercent(usage({ used_usd: 2.5 }))).toBe(25);
    expect(barPercent(usage({ used_usd: 30 }))).toBe(100);
    expect(barPercent(usage({ limit_usd: 0, used_usd: 0 }))).toBe(100);
    expect(barPercent(usage({ unlimited: true, limit_usd: null }))).toBe(0);
  });
});

describe("modelPricePath", () => {
  it("encodes each segment and keeps the slashes the path route captures", () => {
    expect(modelPricePath("global.anthropic.claude-sonnet-5")).toBe(
      "/api/admin/model-prices/global.anthropic.claude-sonnet-5",
    );
    expect(modelPricePath("qwen.qwen3-32b-v1:0")).toBe("/api/admin/model-prices/qwen.qwen3-32b-v1%3A0");
    expect(
      modelPricePath("arn:aws:bedrock:us-east-1:123456789012:inference-profile/global.openai.gpt-6-sol"),
    ).toBe(
      "/api/admin/model-prices/arn%3Aaws%3Abedrock%3Aus-east-1%3A123456789012%3Ainference-profile/global.openai.gpt-6-sol",
    );
    expect(modelPricePath("a+b@c")).toBe("/api/admin/model-prices/a%2Bb%40c");
  });
});

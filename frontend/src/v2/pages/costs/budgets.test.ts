import { describe, expect, it } from "vitest";

import type { AgentCoreRates, BudgetLevelInfo, BudgetPerson, ModelPriceEntry } from "../../../lib/api";
import {
  amountFromInput,
  amountToInput,
  assignDraftFrom,
  assignDraftValid,
  assignmentPayload,
  cacheRate,
  isPriceKey,
  levelCreatePayload,
  levelDraftFrom,
  levelDraftValid,
  levelPatchPayload,
  mergeUnpriced,
  peopleSummary,
  previewLimit,
  priceDraftFrom,
  priceDraftValid,
  pricePayload,
  priceRows,
  ratePatch,
  rateRows,
  sortPeople,
  unitKey,
} from "./budgets";

function level(over: Partial<BudgetLevelInfo> = {}): BudgetLevelInfo {
  return {
    id: "l1",
    name: "Standard",
    monthly_budget_usd: 20,
    is_default: false,
    user_count: 0,
    created_by: null,
    created_by_name: null,
    created_at: null,
    updated_at: null,
    ...over,
  };
}

function person(over: Partial<BudgetPerson> = {}): BudgetPerson {
  return {
    user_id: "u1",
    username: "alice",
    role: "member",
    status: "active",
    level: null,
    assigned_level_id: null,
    override_usd: null,
    basis: "unlimited",
    limit_usd: null,
    used_usd: 0,
    remaining_usd: null,
    estimated_usd: 0,
    estimated_share: 0,
    percent_used: null,
    unlimited: true,
    top_source: null,
    by_source: [],
    ...over,
  };
}

function price(model_id: string, over: Partial<ModelPriceEntry> = {}): ModelPriceEntry {
  return {
    model_id,
    rates: { input: 3, output: 15, cache_read: null, cache_write: null },
    base: null,
    overridden: false,
    updated_by: null,
    updated_at: null,
    ...over,
  };
}

describe("amounts", () => {
  it("accepts 0 to the cap and refuses the rest", () => {
    expect(amountFromInput("0")).toBe(0);
    expect(amountFromInput(" 12.5 ")).toBe(12.5);
    expect(amountFromInput("1000000")).toBe(1_000_000);
    expect(amountFromInput("1000000.01")).toBeNull();
    expect(amountFromInput("-1")).toBeNull();
    expect(amountFromInput("abc")).toBeNull();
    expect(amountFromInput("")).toBeNull();
    expect(amountFromInput("Infinity")).toBeNull();
    expect(amountFromInput("101", 100)).toBeNull();
  });

  it("types a stored amount without float noise", () => {
    expect(amountToInput(0.1 + 0.2)).toBe("0.3");
    expect(amountToInput(20)).toBe("20");
    expect(amountToInput(null)).toBe("");
  });
});

describe("levels", () => {
  it("creates from a draft", () => {
    const draft = { name: "  Pro ", amount: "50", isDefault: true };
    expect(levelDraftValid(draft)).toBe(true);
    expect(levelCreatePayload(draft)).toEqual({ name: "Pro", monthly_budget_usd: 50, is_default: true });
  });

  it("refuses an empty name, a long name or a bad amount", () => {
    expect(levelDraftValid({ name: " ", amount: "5", isDefault: false })).toBe(false);
    expect(levelDraftValid({ name: "x".repeat(65), amount: "5", isDefault: false })).toBe(false);
    expect(levelDraftValid({ name: "ok", amount: "", isDefault: false })).toBe(false);
    expect(levelDraftValid({ name: "ok", amount: "-2", isDefault: false })).toBe(false);
  });

  it("patches only what changed", () => {
    const row = level({ name: "Standard", monthly_budget_usd: 20, is_default: false });
    expect(levelPatchPayload(row, levelDraftFrom(row))).toEqual({});
    expect(levelPatchPayload(row, { name: "Standard", amount: "25", isDefault: false })).toEqual({ monthly_budget_usd: 25 });
    expect(levelPatchPayload(row, { name: "Std", amount: "20", isDefault: true })).toEqual({ name: "Std", is_default: true });
  });
});

describe("assignment", () => {
  it("drafts from a people row or a user row", () => {
    expect(assignDraftFrom({ assigned_level_id: "l1", override_usd: 7.5 })).toEqual({ levelId: "l1", override: "7.5" });
    expect(assignDraftFrom({ budget_level_id: null, budget_override_usd: null })).toEqual({ levelId: "", override: "" });
  });

  it("sends the full assignment, nulls clearing", () => {
    expect(assignmentPayload({ levelId: "l1", override: "" })).toEqual({ level_id: "l1", override_usd: null });
    expect(assignmentPayload({ levelId: "", override: "3" })).toEqual({ level_id: null, override_usd: 3 });
    expect(assignmentPayload({ levelId: "", override: "0" })).toEqual({ level_id: null, override_usd: 0 });
    expect(assignDraftValid({ levelId: "", override: "x" })).toBe(false);
    expect(() => assignmentPayload({ levelId: "", override: "-1" })).toThrow();
  });

  const levels = [level({ id: "std", name: "Standard", monthly_budget_usd: 20 }), level({ id: "pro", name: "Pro", monthly_budget_usd: 100 })];
  const withDefault = [levels[0], { ...levels[1], is_default: true }];

  it("previews override > level > default > unlimited", () => {
    expect(previewLimit("member", { levelId: "std", override: "5" }, levels)).toEqual({
      limit: 5,
      basis: "override",
      levelName: "Standard",
    });
    expect(previewLimit("member", { levelId: "std", override: "" }, levels)).toEqual({
      limit: 20,
      basis: "level",
      levelName: "Standard",
    });
    expect(previewLimit("member", { levelId: "", override: "" }, withDefault)).toEqual({
      limit: 100,
      basis: "default_level",
      levelName: "Pro",
    });
    expect(previewLimit("member", { levelId: "", override: "" }, levels)).toEqual({
      limit: null,
      basis: "unlimited",
      levelName: null,
    });
  });

  it("keeps an admin exempt unless assigned, and never on the default level", () => {
    expect(previewLimit("admin", { levelId: "", override: "" }, withDefault).basis).toBe("exempt_admin");
    expect(previewLimit("admin", { levelId: "std", override: "" }, withDefault)).toMatchObject({ limit: 20, basis: "level" });
    expect(previewLimit("admin", { levelId: "", override: "1" }, withDefault)).toMatchObject({ limit: 1, basis: "override" });
  });
});

describe("the people table", () => {
  const rows = [
    person({ user_id: "a", username: "alice", used_usd: 5, remaining_usd: 5, unlimited: false, limit_usd: 10, percent_used: 50 }),
    person({ user_id: "b", username: "bob", used_usd: 9, remaining_usd: 1, unlimited: false, limit_usd: 10, percent_used: 90 }),
    person({ user_id: "c", username: "carol", used_usd: 12, remaining_usd: 0, unlimited: false, limit_usd: 10, percent_used: 120 }),
    person({ user_id: "d", username: "dave", used_usd: 30, estimated_usd: 6 }),
  ];

  it("sorts by spend, remaining (unlimited last) and name, and filters", () => {
    expect(sortPeople(rows, { key: "used", dir: "desc" }).map((p) => p.username)).toEqual(["dave", "carol", "bob", "alice"]);
    expect(sortPeople(rows, { key: "remaining", dir: "asc" }).map((p) => p.username)).toEqual(["carol", "bob", "alice", "dave"]);
    expect(sortPeople(rows, { key: "person", dir: "desc" }).map((p) => p.username)).toEqual(["dave", "carol", "bob", "alice"]);
    expect(sortPeople(rows, { key: "used", dir: "desc" }, "AL").map((p) => p.username)).toEqual(["alice"]);
  });

  it("summarizes spend, limits and who is near or over", () => {
    const summary = peopleSummary(rows, [{ ...person(), used_usd: 4, estimated_usd: 2 }]);
    expect(summary.totalUsd).toBe(60);
    expect(summary.estimatedUsd).toBe(8);
    expect(summary.estimatedShare).toBeCloseTo(8 / 60);
    expect(summary.budgeted).toBe(3);
    expect(summary.warn).toBe(1);
    expect(summary.over).toBe(1);
    expect(peopleSummary([], []).estimatedShare).toBe(0);
  });
});

describe("model prices", () => {
  it("accepts model-id shaped keys only", () => {
    expect(isPriceKey("global.anthropic.claude-sonnet-5")).toBe(true);
    expect(isPriceKey("arn:aws:bedrock:us-east-1:1:inference-profile/x")).toBe(true);
    expect(isPriceKey("a")).toBe(false);
    expect(isPriceKey("has space")).toBe(false);
    expect(isPriceKey("-leading")).toBe(false);
  });

  it("needs input and output; empty cache rates are left to the server's factors", () => {
    const draft = { ...priceDraftFrom("qwen.qwen3-4b", null), input: "0.1", output: "0.4" };
    expect(priceDraftValid(draft)).toBe(true);
    expect(pricePayload(draft)).toEqual({ input: 0.1, output: 0.4 });
    expect(pricePayload({ ...draft, cache_read: "0.01", cache_write: "0" })).toEqual({
      input: 0.1,
      output: 0.4,
      cache_read: 0.01,
      cache_write: 0,
    });
    expect(priceDraftValid({ ...draft, output: "" })).toBe(false);
    expect(priceDraftValid({ ...draft, cache_read: "-1" })).toBe(false);
    expect(priceDraftValid({ ...draft, input: "10001" })).toBe(false);
    expect(priceDraftValid({ ...draft, modelId: "x" })).toBe(false);
  });

  it("drafts from an entry's rates", () => {
    expect(priceDraftFrom("m1", { input: 3, output: 15, cache_read: 0.3, cache_write: null })).toEqual({
      modelId: "m1",
      input: "3",
      output: "15",
      cache_read: "0.3",
      cache_write: "",
    });
  });

  it("derives a missing cache rate from the input rate", () => {
    const factors = { cache_read: 0.1, cache_write: 1.25 };
    expect(cacheRate({ input: 3, output: 15, cache_read: null, cache_write: null }, "cache_read", factors)).toEqual({
      value: 0.3,
      derived: true,
    });
    expect(cacheRate({ input: 3, output: 15, cache_read: null, cache_write: null }, "cache_write", factors)).toEqual({
      value: 3.75,
      derived: true,
    });
    expect(cacheRate({ input: 3, output: 15, cache_read: 0.5, cache_write: null }, "cache_read", factors)).toEqual({
      value: 0.5,
      derived: false,
    });
    expect(cacheRate(null, "cache_read", factors)).toEqual({ value: null, derived: false });
  });

  it("merges span-seen unpriced models into the server's list", () => {
    expect(
      mergeUnpriced([{ model_id: "b", seen_in: ["agents"] }], ["a", "b", "(unknown model)", ""]),
    ).toEqual([
      { model_id: "a", seen_in: ["spans"] },
      { model_id: "b", seen_in: ["agents", "spans"] },
    ]);
  });

  it("lists unpriced models first, then overrides, then the rest", () => {
    const prices = [price("z-model"), price("a-model"), price("m-model", { overridden: true })];
    const rows = priceRows(prices, [{ model_id: "q-unpriced", seen_in: ["agents"] }]);
    expect(rows.map((r) => r.model_id)).toEqual(["q-unpriced", "m-model", "a-model", "z-model"]);
    expect(rows[0]).toMatchObject({ entry: null, unpriced: ["agents"] });
    expect(priceRows(prices, [], "Z-MO").map((r) => r.model_id)).toEqual(["z-model"]);
  });

  it("does not list a span id twice once it has a price", () => {
    const rows = priceRows([price("now-priced")], [{ model_id: "now-priced", seen_in: ["spans"] }]);
    expect(rows).toHaveLength(1);
    expect(rows[0].unpriced).toBeNull();
  });
});

describe("AgentCore rates", () => {
  const view: AgentCoreRates = {
    rates: { runtime_v1_gb_hour: 0.00945, gateway_call: 0.00001 },
    defaults: { runtime_v1_gb_hour: 0.00945, gateway_call: 0.000005 },
    units: { runtime_v1_gb_hour: "GB-hour", gateway_call: "call" },
    calibration: { runtime_idle_timeout_seconds: 900 },
    calibration_defaults: { runtime_idle_timeout_seconds: 900 },
    calibration_units: { runtime_idle_timeout_seconds: "seconds" },
    updated_by: null,
    updated_at: null,
  };

  it("marks the keys that differ from the seeded default", () => {
    expect(rateRows(view, "rates")).toEqual([
      { group: "rates", key: "runtime_v1_gb_hour", value: 0.00945, default: 0.00945, unit: "GB-hour", changed: false },
      { group: "rates", key: "gateway_call", value: 0.00001, default: 0.000005, unit: "call", changed: true },
    ]);
    expect(rateRows(view, "calibration")[0]).toMatchObject({ key: "runtime_idle_timeout_seconds", unit: "seconds", changed: false });
  });

  it("puts one key (null resets) under its group", () => {
    expect(ratePatch("rates", "gateway_call", 0.00002)).toEqual({ rates: { gateway_call: 0.00002 } });
    expect(ratePatch("calibration", "runtime_harness_gb", null)).toEqual({ calibration: { runtime_harness_gb: null } });
  });

  it("maps the server's unit strings to copy keys", () => {
    expect(unitKey("vCPU-hour")).toBe("v2.models.rates.unit.vcpu_hour");
    expect(unitKey("1K input tokens")).toBe("v2.models.rates.unit.1k_input_tokens");
    expect(unitKey("tokens per evaluation")).toBe("v2.models.rates.unit.tokens_per_evaluation");
  });
});

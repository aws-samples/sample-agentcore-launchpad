import { describe, expect, it } from "vitest";

import type { AlertRuleInfo } from "../../../lib/api";
import {
  ALERT_KINDS,
  coerceWindow,
  createPayload,
  draftFromRule,
  draftValid,
  emptyDraft,
  fixedWindowKey,
  fmtRuleValue,
  fmtUsd,
  kindUsesWindow,
  NATURAL_COMPARISON,
  patchPayload,
  ruleSignal,
  ruleStateKey,
  thresholdFromInput,
  thresholdToInput,
  totalSessions,
  windowOptions,
} from "./alertRules";

function rule(over: Partial<AlertRuleInfo> = {}): AlertRuleInfo {
  return {
    id: "r1",
    kind: "error_rate",
    name: "errors",
    comparison: "above",
    threshold: 0.05,
    window: "24h",
    enabled: true,
    has_webhook: false,
    state: "unknown",
    last_value: null,
    last_detail: null,
    last_checked_at: null,
    last_fired_at: null,
    last_notified_at: null,
    created_by: null,
    created_by_name: null,
    created_at: null,
    updated_at: null,
    ...over,
  };
}

describe("kinds and windows", () => {
  it("mirrors the backend's natural comparison", () => {
    expect(ALERT_KINDS.map((k) => NATURAL_COMPARISON[k])).toEqual(["above", "above", "below", "above", "above"]);
  });

  it("offers only the windows a kind accepts", () => {
    expect(windowOptions("error_rate")).toEqual(["1h", "6h", "24h", "7d"]);
    expect(windowOptions("latency_p95_ms")).toEqual(["1h", "6h", "24h", "7d"]);
    expect(windowOptions("spend_usd")).toEqual(["1h", "6h", "24h", "7d", "30d"]);
    expect(windowOptions("online_quality")).toEqual([]);
    expect(windowOptions("cost_mtd_usd")).toEqual([]);
    expect(kindUsesWindow("online_quality")).toBe(false);
    expect(kindUsesWindow("spend_usd")).toBe(true);
  });

  it("names the fixed window of the kinds that ignore it", () => {
    expect(fixedWindowKey("online_quality")).toBe("v2.alerts.windowFixed24h");
    expect(fixedWindowKey("cost_mtd_usd")).toBe("v2.alerts.windowMonth");
    expect(fixedWindowKey("error_rate")).toBeNull();
  });

  it("coerces a 30d window when switching to a dashboard kind", () => {
    expect(coerceWindow("error_rate", "30d")).toBe("24h");
    expect(coerceWindow("spend_usd", "30d")).toBe("30d");
    expect(coerceWindow("error_rate", "7d")).toBe("7d");
  });
});

describe("formatting", () => {
  it("formats money, with sub-cent precision and a dash for unpriced", () => {
    expect(fmtUsd(null)).toBe("—");
    expect(fmtUsd(0)).toBe("$0.00");
    expect(fmtUsd(0.0075)).toBe("$0.0075");
    expect(fmtUsd(1234.5)).toBe("$1,234.50");
  });

  it("formats a value per kind", () => {
    expect(fmtRuleValue("error_rate", 0.05)).toBe("5%");
    expect(fmtRuleValue("error_rate", 0.12345)).toBe("12.35%");
    expect(fmtRuleValue("latency_p95_ms", 2345.6)).toBe("2,346 ms");
    expect(fmtRuleValue("online_quality", 0.8)).toBe("0.80");
    expect(fmtRuleValue("spend_usd", 12)).toBe("$12.00");
    expect(fmtRuleValue("cost_mtd_usd", 105.05)).toBe("$105.05");
    expect(fmtRuleValue("error_rate", null)).toBe("—");
  });
});

describe("thresholds", () => {
  it("types an error rate in percent and stores the fraction", () => {
    expect(thresholdFromInput("error_rate", "5")).toBe(0.05);
    expect(thresholdToInput("error_rate", 0.07)).toBe("7");
    expect(thresholdFromInput("error_rate", "101")).toBeNull();
  });

  it("rejects empty, negative, non-numeric and out-of-range values", () => {
    expect(thresholdFromInput("spend_usd", "")).toBeNull();
    expect(thresholdFromInput("spend_usd", "-1")).toBeNull();
    expect(thresholdFromInput("spend_usd", "abc")).toBeNull();
    expect(thresholdFromInput("online_quality", "1.2")).toBeNull();
    expect(thresholdFromInput("online_quality", "0.7")).toBe(0.7);
    expect(thresholdFromInput("latency_p95_ms", " 3000 ")).toBe(3000);
  });
});

describe("signals", () => {
  it("maps state and enablement onto the V2 signal vocabulary", () => {
    expect(ruleSignal({ enabled: true, state: "firing" })).toBe("act");
    expect(ruleSignal({ enabled: true, state: "ok" })).toBe("ok");
    expect(ruleSignal({ enabled: true, state: "unknown" })).toBe("wait");
    expect(ruleSignal({ enabled: false, state: "firing" })).toBe("off");
    expect(ruleStateKey({ enabled: false, state: "ok" })).toBe("v2.alerts.state.disabled");
    expect(ruleStateKey({ enabled: true, state: "firing" })).toBe("v2.alerts.state.firing");
  });
});

describe("create payload", () => {
  it("sends the window only for kinds that use it, and the webhook only when typed", () => {
    expect(createPayload({ ...emptyDraft("error_rate"), threshold: "5", name: " e " })).toEqual({
      kind: "error_rate",
      name: "e",
      threshold: 0.05,
      window: "24h",
    });
    expect(createPayload({ ...emptyDraft("cost_mtd_usd"), threshold: "100", window: "7d" })).toEqual({
      kind: "cost_mtd_usd",
      name: "",
      threshold: 100,
    });
    expect(
      createPayload({ ...emptyDraft("spend_usd"), threshold: "10", window: "30d", webhook: " https://hooks.example.com/x " }),
    ).toEqual({ kind: "spend_usd", name: "", threshold: 10, window: "30d", webhook_url: "https://hooks.example.com/x" });
  });

  it("is invalid without a threshold", () => {
    expect(draftValid(emptyDraft())).toBe(false);
    expect(draftValid({ ...emptyDraft(), threshold: "3" })).toBe(true);
  });
});

describe("patch payload", () => {
  it("sends nothing when nothing changed", () => {
    const r = rule({ threshold: 0.07 });
    expect(patchPayload(r, draftFromRule(r))).toEqual({});
  });

  it("sends only the changed fields", () => {
    const r = rule();
    expect(patchPayload(r, { ...draftFromRule(r), threshold: "10", window: "7d", name: "x" })).toEqual({
      name: "x",
      threshold: 0.1,
      window: "7d",
    });
  });

  it("never sends a window for a kind that ignores it", () => {
    const r = rule({ kind: "online_quality", comparison: "below", threshold: 0.7 });
    expect(patchPayload(r, { ...draftFromRule(r), window: "7d" })).toEqual({});
  });

  it("keeps, replaces or removes an existing webhook", () => {
    const r = rule({ has_webhook: true });
    const base = draftFromRule(r);
    expect(patchPayload(r, { ...base, webhookMode: "keep", webhook: "https://ignored.example.com" })).toEqual({});
    expect(patchPayload(r, { ...base, webhookMode: "replace", webhook: "https://new.example.com" })).toEqual({
      webhook_url: "https://new.example.com",
    });
    expect(patchPayload(r, { ...base, webhookMode: "remove" })).toEqual({ webhook_url: null });
    // replacing with nothing typed is not submittable
    expect(draftValid({ ...base, webhookMode: "replace" }, r)).toBe(false);
  });

  it("adds a webhook to a rule without one only when a URL is typed", () => {
    const r = rule();
    expect(patchPayload(r, draftFromRule(r))).toEqual({});
    expect(patchPayload(r, { ...draftFromRule(r), webhook: "https://a.example.com" })).toEqual({
      webhook_url: "https://a.example.com",
    });
  });
});

describe("totalSessions", () => {
  it("sums the per-person sessions and flags the 1000-row cap", () => {
    expect(totalSessions([])).toEqual({ count: 0, capped: false });
    expect(
      totalSessions([
        { actor: "a", attributed: true, sessions: 3, tokens: 1, est_cost_usd: 0 },
        { actor: "—", attributed: false, sessions: 997, tokens: 1, est_cost_usd: 0 },
      ]),
    ).toEqual({ count: 1000, capped: true });
  });
});

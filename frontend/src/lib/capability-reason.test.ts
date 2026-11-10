import type { TFunction } from "i18next";
import { describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { capabilityReasonText } from "./capability-reason";

// a `t` with i18next's missing-key contract: the translation when the key exists,
// otherwise `defaultValue` when given, otherwise the key itself
function fakeT(table: Record<string, string>): TFunction {
  return ((key: string, options?: { defaultValue?: string }) =>
    table[key] ?? options?.defaultValue ?? key) as unknown as TFunction;
}

const T = fakeT({
  "canaryPage.reason.system-managed": "System-managed presets cannot be the subject of a canary.",
  "canaryPage.reason.not-runtime": "Target canaries require an AgentCore Runtime agent.",
  "canaryPage.reason.unknown": "Not eligible for a canary (no reason reported).",
});

describe("capabilityReasonText", () => {
  it("translates a known reason code", () => {
    expect(
      capabilityReasonText(T, "canaryPage", { eligible: false, reason_code: "system-managed", reason: "server text" }),
    ).toBe("System-managed presets cannot be the subject of a canary.");
    expect(capabilityReasonText(T, "canaryPage", { eligible: false, reason_code: "not-runtime", reason: null })).toBe(
      "Target canaries require an AgentCore Runtime agent.",
    );
  });

  it("falls back to the server text when the code has no string (N12-obs35)", () => {
    expect(
      capabilityReasonText(T, "canaryPage", {
        eligible: false,
        reason_code: "brand-new-code",
        reason: "BYOC candidates cannot be minted from an edited spec.",
      }),
    ).toBe("BYOC candidates cannot be minted from an edited spec.");
    // never the raw key
    expect(capabilityReasonText(T, "canaryPage", { eligible: false, reason_code: "brand-new-code", reason: "x" })).not.toContain(
      "canaryPage.reason.",
    );
  });

  it("uses the page's generic string when there is neither a code nor a text", () => {
    expect(capabilityReasonText(T, "canaryPage", { eligible: false, reason_code: null, reason: null })).toBe(
      "Not eligible for a canary (no reason reported).",
    );
    expect(capabilityReasonText(T, "canaryPage", undefined)).toBe("Not eligible for a canary (no reason reported).");
    expect(capabilityReasonText(T, "canaryPage", { eligible: false, reason_code: "", reason: "   " })).toBe(
      "Not eligible for a canary (no reason reported).",
    );
  });

  it("ships a string for every reason code the backend emits, in both locales", () => {
    // mirror of backend/tests/optimization/test_capability_matrix.py (the authoritative
    // list is parsed from optimization/capability.py there); keep in sync when a code is added
    const canaryCodes = ["system-managed", "not-active", "not-runtime", "custom-source-unverified", "a2a", "no-runtime-arn", "unknown"];
    for (const locale of [en, zh]) {
      const canary = (locale as { canaryPage: { reason: Record<string, string> } }).canaryPage.reason;
      for (const code of canaryCodes) expect(canary[code], `canaryPage.reason.${code}`).toBeTruthy();
      expect(canary["not-http-runtime"]).toBeUndefined(); // named a method, not a capability
    }
  });
});

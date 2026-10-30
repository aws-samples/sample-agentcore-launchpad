import { describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { activeNavItem, V2_NAV } from "./nav";

/**
 * Global console structure (2026-10-28): 「策略」 is a TOP-LEVEL sidebar entry at the same level
 * as 「网关」 and 「身份」 (the Run group), gated by the `policy` feature, routed to /v2/policy.
 */
describe("V2_NAV — the Policy entry", () => {
  const run = V2_NAV.find((g) => g.key === "run");
  it("sits in the Run group right after Gateway, with the policy feature", () => {
    expect(run).toBeTruthy();
    const keys = run!.items.map((i) => i.labelKey);
    expect(keys).toContain("nav.identity");
    expect(keys).toContain("nav.gateway");
    expect(keys.indexOf("nav.policy")).toBe(keys.indexOf("nav.gateway") + 1);
    const policy = run!.items.find((i) => i.labelKey === "nav.policy")!;
    expect(policy.to).toBe("/v2/policy");
    expect(policy.feature).toBe("policy");
    expect(policy.admin).toBeUndefined();
    expect(en.nav.policy).toBe("Policy");
    expect(zh.nav.policy).toBe("策略");
  });

  it("is the active entry on every Policy sub-page and never on the Gateway page", () => {
    expect(activeNavItem("/v2/policy", "")?.labelKey).toBe("nav.policy");
    expect(activeNavItem("/v2/policy", "?view=engine&engine=pe_1&tab=gateways")?.labelKey).toBe("nav.policy");
    expect(activeNavItem("/v2/governance", "?view=gateway&gateway=gw_1&section=policy-engine")?.labelKey).toBe("nav.gateway");
  });
});

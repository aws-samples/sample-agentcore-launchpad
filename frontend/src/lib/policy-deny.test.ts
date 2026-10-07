import { describe, expect, it } from "vitest";

import { livePolicyDeny, policyDenyHref, policyIdFrom, restoredPolicyDeny } from "./policy-deny";

const DEFAULT_REASON = "No policy applies to the request (denied by default).";
const POLICY_REASON = "Policy evaluation denied due to launchpad_payout_admin_only-qtv_30w04g";

describe("policyIdFrom", () => {
  it("reads the determining policy", () => {
    expect(policyIdFrom(POLICY_REASON)).toBe("launchpad_payout_admin_only-qtv_30w04g");
  });
  it("is null for a default deny", () => {
    expect(policyIdFrom(DEFAULT_REASON)).toBeNull();
  });
});

describe("livePolicyDeny", () => {
  it("maps the SSE payload and deep-links the gateway's policies", () => {
    const deny = livePolicyDeny({
      tool: "hr-database___create_payout",
      reason: DEFAULT_REASON,
      policy_id: null,
      gateway_id: "launchpad-gw-abc",
    });
    expect(deny).toEqual({
      tool: "hr-database___create_payout",
      reason: DEFAULT_REASON,
      policyId: null,
      gatewayId: "launchpad-gw-abc",
    });
    expect(policyDenyHref(deny)).toBe("/v2/governance?view=gateway&gateway=launchpad-gw-abc&section=policies");
  });

  it("falls back to the landing page without a gateway", () => {
    const deny = livePolicyDeny({ tool: "x", reason: POLICY_REASON, gateway_id: null });
    expect(deny.policyId).toBe("launchpad_payout_admin_only-qtv_30w04g");
    expect(policyDenyHref(deny)).toBe("/v2/governance");
  });
});

describe("restoredPolicyDeny", () => {
  it("rebuilds the card from a history row", () => {
    const deny = restoredPolicyDeny({ text: POLICY_REASON, name: "hr-database___create_payout" });
    expect(deny).toEqual({
      tool: "hr-database___create_payout",
      reason: POLICY_REASON,
      policyId: "launchpad_payout_admin_only-qtv_30w04g",
      gatewayId: null,
    });
    expect(policyDenyHref(deny)).toBe("/v2/governance");
  });
});

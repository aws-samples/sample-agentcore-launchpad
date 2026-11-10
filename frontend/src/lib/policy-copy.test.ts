import { describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zhCN from "../locales/zh-CN/common.json";

/**
 * D20 P1-c3 copy contracts the console promises the backend:
 *  - `apiErrors.policy.undecidable` exists for the 503 the invoke gate raises when the engine
 *    cannot decide (`policy_invoke_fail_mode` closed), beside the older `policy.unavailable`;
 *  - the Decisions page describes a `missing` CloudWatch channel as *not yet confirmed*
 *    (the `policy` bootstrap stage creates the group), never as "unavailable" / "not configured".
 */
describe("policy error copy", () => {
  it("has the undecidable (fail-closed) message in both locales", () => {
    expect(en.apiErrors["policy.undecidable"]).toBe("The policy engine could not decide — the request was refused (fail-closed)");
    expect(zhCN.apiErrors["policy.undecidable"]).toBe("策略引擎无法判定，请求已拒绝（默认拒绝）");
    expect(en.apiErrors["policy.denied"]).toBeTruthy();
    expect(en.apiErrors["policy.compile_failed"]).toBeTruthy();
  });
});

describe("runtime channel copy", () => {
  it("describes `missing` as a mirror not yet confirmed that bootstrap creates", () => {
    const v2 = en.v2.governance.policy.dec.runtimeChannelStatus.missing;
    expect(v2).toMatch(/not confirmed yet/);
    expect(v2).toMatch(/bootstrap/);
    expect(v2).not.toMatch(/unavailable|not configured/);
    expect(zhCN.v2.governance.policy.dec.runtimeChannelStatus.missing).toMatch(/尚未确认/);
    expect(zhCN.v2.governance.policy.dec.runtimeChannelStatus.missing).toMatch(/引导/);
  });
  it("keeps the `unknown` state distinct (no batch shipped yet)", () => {
    expect(en.v2.governance.policy.dec.runtimeChannelStatus.unknown).toMatch(/unknown/);
  });
});

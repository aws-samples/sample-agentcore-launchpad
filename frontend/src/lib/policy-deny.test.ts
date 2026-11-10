import { describe, expect, it } from "vitest";

import type { PolicyDecision } from "./api";
import { decisionPolicyDeny, livePolicyDeny, policyDecisionsHref, policyDenyHref, policyIdFrom, policyLabel, restoredPolicyDeny } from "./policy-deny";

const DEFAULT_REASON = "No policy applies to the request (denied by default).";
// the Global denial signature, now emitted by the D20 runtime guard with a `pol_…:v<n>` id
const POLICY_REASON =
  "Tool Execution Denied: Tool call not allowed due to policy enforcement [Policy evaluation denied due to pol_payout1:v2]";

describe("policyIdFrom", () => {
  it("reads the determining policy and strips the version annotation", () => {
    expect(policyIdFrom(POLICY_REASON)).toBe("pol_payout1");
  });
  it("is null for a default deny", () => {
    expect(policyIdFrom(DEFAULT_REASON)).toBeNull();
  });
});

describe("livePolicyDeny", () => {
  it("maps the SSE payload and deep-links the policy editor", () => {
    const deny = livePolicyDeny({
      tool: "hr-database___create_payout",
      reason: DEFAULT_REASON,
      policy_id: null,
      decision_id: "dec_1",
    });
    expect(deny).toEqual({
      kind: "tool",
      tool: "hr-database___create_payout",
      reason: DEFAULT_REASON,
      policyId: null,
      decisionId: "dec_1",
    });
    expect(policyDenyHref(deny)).toBe("/v2/policy");
  });
  it("prefers the event's policy id and links its editor", () => {
    const deny = livePolicyDeny({ tool: "x", reason: POLICY_REASON, policy_id: "pol_ab12:v3" });
    expect(deny.policyId).toBe("pol_ab12");
    expect(policyDenyHref(deny)).toBe("/v2/policy?policy=pol_ab12");
  });
});

// Global parity (amendment 9): there is no invoke denial — policies decide tool calls only, so
// the deny helper has exactly one kind (`tool`); the Decisions hrefs are unchanged
// N17-obs507: the Gateway names every determining policy; the hub resolves their names
describe("livePolicyDeny with policy_ids / policy_names (N17-obs507)", () => {
  it("lists every policy, names them (an unknown id names itself) and keeps the first as policyId", () => {
    const deny = livePolicyDeny({
      tool: "hr-database___create_payout", reason: "Tool execution failed: Tool Execution Denied: Tool call not allowed due to policy enforcement [Policy evaluation denied due to pol_a, pol_b]",
      policy_id: "pol_a", policy_ids: ["pol_a", "pol_b"], policy_names: { pol_a: "members may call tools" },
      policy_name: "members may call tools", source: "gateway", stage: null,
    });
    expect(deny.policyId).toBe("pol_a");
    expect(deny.policyIds).toEqual(["pol_a", "pol_b"]);
    expect(deny.policyNames).toEqual(["members may call tools", "pol_b"]);
    expect(policyLabel(deny)).toBe("members may call tools、pol_b");
    expect(deny).not.toHaveProperty("stage");
    // without the list the card reads as before
    const single = livePolicyDeny({ tool: "t", reason: POLICY_REASON, policy_id: "pol_payout1", policy_name: "payout cap" });
    expect(single).not.toHaveProperty("policyIds");
    expect(policyLabel(single)).toBe("payout cap");
    expect(policyLabel(livePolicyDeny({ tool: "t", reason: DEFAULT_REASON }))).toBeNull();
  });
});

describe("hrefs", () => {
  it("links the determining policy's editor and its engine's Decisions tab", () => {
    const deny = livePolicyDeny({ tool: "x", reason: POLICY_REASON, policy_id: "pol_payout1:v2", decision_id: "dec_42" });
    // without an engine id the Policy page resolves the engine that owns the policy
    expect(policyDenyHref(deny)).toBe("/v2/policy?policy=pol_payout1");
    expect(policyDecisionsHref(deny)).toBe("/v2/policy?policy=pol_payout1&tab=decisions");
    expect(policyDecisionsHref(null)).toBe("/v2/policy");
    // the frame names the engine attached to the deciding gateway: link straight to it
    const direct = livePolicyDeny({ tool: "x", reason: POLICY_REASON, policy_id: "pol_payout1:v2", decision_id: "dec_42", engine_id: "pe_hr" });
    expect(direct.engineId).toBe("pe_hr");
    expect(policyDenyHref(direct)).toBe("/v2/policy?view=policy&engine=pe_hr&policy=pol_payout1");
    expect(policyDecisionsHref(direct)).toBe("/v2/policy?view=engine&engine=pe_hr&tab=decisions");
    expect(policyDenyHref({ ...direct, policyId: null })).toBe("/v2/policy?view=engine&engine=pe_hr");
  });
});

describe("restoredPolicyDeny", () => {
  it("rebuilds the card from a history row", () => {
    const deny = restoredPolicyDeny({ text: POLICY_REASON, name: "hr-database___create_payout" });
    expect(deny).toEqual({
      kind: "tool",
      tool: "hr-database___create_payout",
      reason: POLICY_REASON,
      policyId: "pol_payout1",
      decisionId: null,
    });
    // a row an older build wrote for the retired invoke gate carries no tool — still a tool card
    expect(restoredPolicyDeny({ text: "denied", name: null })).toMatchObject({ kind: "tool", tool: "" });
  });
});


// A6 deny card: the denying policy's NAME and the engine STAGE ride on the frame / envelope /
// history row / Decisions row; an unknown stage or source is never shown
describe("A6 facts — policy name, stage, source", () => {
  it("livePolicyDeny keeps the Gateway interceptor's facts and drops unknown values", () => {
    const deny = livePolicyDeny({
      tool: "hr-database___create_payout", reason: POLICY_REASON, policy_id: "pol_payout1:v2",
      decision_id: "gw-1", policy_name: "payout cap", stage: "bt", source: "gateway",
    });
    expect(deny).toMatchObject({ policyId: "pol_payout1", policyName: "payout cap", stage: "bt", source: "gateway" });
    const odd = livePolicyDeny({ tool: "x", reason: DEFAULT_REASON, stage: "zz" as never, source: "nope" as never, policy_name: "" });
    expect(odd).not.toHaveProperty("stage");
    expect(odd).not.toHaveProperty("source");
    expect(odd).not.toHaveProperty("policyName");
  });
  it("`invoke` is not a stage any more: a frame naming it drops the value", () => {
    const deny = livePolicyDeny({ tool: "x", reason: POLICY_REASON, policy_id: "pol_payout1", stage: "invoke" as never });
    expect(deny).not.toHaveProperty("stage");
  });
  it("restoredPolicyDeny replays the facts the history row kept", () => {
    const deny = restoredPolicyDeny({
      text: POLICY_REASON, name: "hr-database___create_payout",
      policy: { policy_id: "pol_payout1", policy_name: "payout cap", stage: "a", source: "runtime", decision_id: "d9", mode: "enforce" },
    });
    expect(deny).toEqual({
      kind: "tool", tool: "hr-database___create_payout", reason: POLICY_REASON, policyId: "pol_payout1",
      decisionId: "d9", mode: "enforce", policyName: "payout cap", stage: "a", source: "runtime",
    });
    expect(restoredPolicyDeny({ text: DEFAULT_REASON, name: null })).toEqual({
      kind: "tool", tool: "", reason: DEFAULT_REASON, policyId: null, decisionId: null,
    });
  });
  it("decisionPolicyDeny builds the card from a Decisions row and the workspace's names", () => {
    const row: PolicyDecision = {
      id: "g1", request_id: null, source: "gateway", channel: "chat", principal_kind: "user",
      principal_id: "hr-intern", principal_label: "hr-intern", action: "call_tool", resource_kind: "tool",
      resource_id: "hr-database___create_payout", agent_id: "agt_hr", session_id: null, stage: "bt",
      decision: "deny", mode: "enforce", policy_ids: ["pol_payout1"], policy_versions: { pol_payout1: 2 },
      bundle_version: null, reason: "Policy evaluation denied due to pol_payout1", latency_ms: 4,
      created_at: "2026-10-27T09:00:00+08:00",
    };
    expect(decisionPolicyDeny(row, { pol_payout1: "payout cap" })).toEqual({
      kind: "tool", tool: "hr-database___create_payout", reason: row.reason, policyId: "pol_payout1",
      decisionId: "g1", agentId: "agt_hr", mode: "enforce", policyName: "payout cap", stage: "bt", source: "gateway",
      // N17-obs509: the row's principal and every determining policy
      principal: { name: "hr-intern", role: "user" }, policyIds: ["pol_payout1"], policyNames: ["payout cap"],
    });
    // two determining policies: both listed in order, named when the list knows them
    const two = decisionPolicyDeny({ ...row, policy_ids: ["pol_payout1:v2", "pol_members"] }, { pol_payout1: "payout cap" });
    expect(two.policyIds).toEqual(["pol_payout1", "pol_members"]);
    expect(two.policyNames).toEqual(["payout cap", "pol_members"]);
    expect(policyLabel(two)).toBe("payout cap、pol_members");
    expect(two.principal).toEqual({ name: "hr-intern", role: "user" });
    // a HISTORIC hub row (the retired invoke gate) named an agent, not a tool: the card keeps the
    // source, has no stage, and a log_only observation is marked
    const hub = decisionPolicyDeny({ ...row, id: "h1", source: "hub", stage: undefined, action: "invoke", resource_kind: "agent",
                                     resource_id: "agt_hr", decision: "would_deny", mode: "log_only", policy_ids: [] });
    expect(hub).toMatchObject({ kind: "tool", tool: "", policyId: "pol_payout1", source: "hub", observed: true });
    expect(hub).not.toHaveProperty("stage");
    expect(hub).not.toHaveProperty("policyName");
  });
});

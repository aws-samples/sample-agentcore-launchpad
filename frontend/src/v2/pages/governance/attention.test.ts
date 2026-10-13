import { describe, expect, it } from "vitest";

import type { GovernanceGatewaySummary, GovernancePolicyEngine } from "../../../lib/api";
import { gatewayAttention, govSignal } from "./attention";

function gw(id: string, over: Partial<GovernanceGatewaySummary> = {}): GovernanceGatewaySummary {
  return {
    id,
    arn: `arn:aws:bedrock-agentcore:us-west-2:123456789012:gateway/${id}`,
    name: `gw-${id}`,
    description: "",
    status: "READY",
    status_reasons: [],
    protocol_type: "MCP",
    authorizer_type: "AWS_IAM",
    url: null,
    role_arn: null,
    managed: true,
    target_count: 0,
    targets: [],
    policy_engine: null,
    shared_gateways: [],
    shared_engine: false,
    attachability: { attachable: true, reason: null, auth_type: "aws_iam" },
    policy_test_available: false,
    updated_at: null,
    ...over,
  };
}

const engine = (over: Partial<GovernancePolicyEngine>): GovernancePolicyEngine => ({
  id: "pe-1",
  arn: "arn:pe-1",
  name: "engine",
  status: "ACTIVE",
  status_reasons: [],
  updated_at: null,
  mode: "ENFORCE",
  missing: false,
  ...over,
});

const target = (name: string, status: string) => ({ id: name, name, status, description: "" });

describe("gateway attention", () => {
  it("is quiet for a READY gateway with healthy targets and an enforcing engine", () => {
    const healthy = gw("a", { policy_engine: engine({}), targets: [target("t1", "READY")] });
    expect(gatewayAttention([healthy, gw("b")])).toEqual([]);
    expect(gatewayAttention([])).toEqual([]);
  });

  it("flags a failed gateway as act and a settling one as wait, with the reasons", () => {
    const [failed] = gatewayAttention([gw("a", { status: "FAILED", status_reasons: ["role missing", "bad url"] })]);
    expect(failed).toMatchObject({
      key: "a:notReady",
      kind: "notReady",
      signal: "act",
      gatewayId: "a",
      values: { name: "gw-a", detail: "role missing; bad url" },
    });
    expect(failed.section).toBeUndefined();
    const [settling] = gatewayAttention([gw("b", { status: "UPDATING" })]);
    expect(settling).toMatchObject({ kind: "notReady", signal: "wait", values: { detail: "UPDATING" } });
  });

  it("does not queue an unknown (muted) or ENFORCE status", () => {
    expect(govSignal("ENFORCE")).toBe("info");
    expect(govSignal("DELETED")).toBe("off");
    expect(gatewayAttention([gw("a", { status: "DELETED" })])).toEqual([]);
  });

  it("sends a deleted engine to policies and never also reports its LOG_ONLY mode", () => {
    const entries = gatewayAttention([gw("a", { policy_engine: engine({ missing: true, mode: "LOG_ONLY", id: "pe-x" }) })]);
    expect(entries).toEqual([
      {
        key: "a:engineMissing",
        kind: "engineMissing",
        signal: "act",
        gatewayId: "a",
        section: "policies",
        values: { name: "gw-a", engine: "pe-x" },
      },
    ]);
  });

  it("sends a LOG_ONLY engine to decisions as wait", () => {
    const entries = gatewayAttention([gw("a", { policy_engine: engine({ mode: "LOG_ONLY" }) })]);
    expect(entries).toEqual([
      { key: "a:logOnly", kind: "logOnly", signal: "wait", gatewayId: "a", section: "decisions", values: { name: "gw-a" } },
    ]);
  });

  it("counts the unhealthy targets in one row: act if any failed, else wait", () => {
    const targets = [target("ok", "READY"), target("t1", "SYNCHRONIZING"), target("t2", "FAILED"), target("odd", "WHATEVER")];
    const [row] = gatewayAttention([gw("a", { targets })]);
    expect(row).toMatchObject({
      key: "a:targets",
      kind: "targets",
      signal: "act",
      section: "targets",
      values: { name: "gw-a", count: 2, names: "t1 · SYNCHRONIZING  ·  t2 · FAILED" },
    });
    const [waiting] = gatewayAttention([gw("b", { targets: [target("t1", "UPDATE_PENDING_AUTH")] })]);
    expect(waiting).toMatchObject({ signal: "wait", values: { count: 1 } });
  });

  it("puts every act before every wait and keeps list order otherwise", () => {
    const entries = gatewayAttention([
      gw("a", { status: "CREATING", policy_engine: engine({ mode: "LOG_ONLY" }) }),
      gw("b", { status: "FAILED", targets: [target("t", "UPDATING")] }),
      gw("c", { policy_engine: engine({ missing: true }), targets: [target("t", "ERROR")] }),
    ]);
    expect(entries.map((e) => e.key)).toEqual([
      "b:notReady",
      "c:engineMissing",
      "c:targets",
      "a:notReady",
      "a:logOnly",
      "b:targets",
    ]);
  });
});

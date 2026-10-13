import { describe, expect, it } from "vitest";

import type { OnlineEvalConfigRow } from "../../../lib/api";
import { onlineAttention, onlineAttentionKind } from "./attention";

function cfg(id: string, over: Partial<OnlineEvalConfigRow> = {}): OnlineEvalConfigRow {
  return {
    config_id: id,
    arn: null,
    name: `cfg-${id}`,
    description: "",
    owner: "agent",
    status: "ACTIVE",
    execution_status: "ENABLED",
    failure_reason: null,
    agent_id: "a1",
    agent_name: "agent-1",
    matched_agent: null,
    detailed: true,
    mode: "scores",
    evaluators: ["Builtin.Helpfulness"],
    sampling_percentage: 10,
    session_timeout_minutes: 15,
    filter_count: 0,
    filters: [],
    data_source: { log_groups: [], service_name: null },
    insights: [],
    clustering_frequencies: [],
    execution_role_arn: null,
    results_log_group: "/aws/x",
    duplicate_enabled: false,
    created_at: null,
    updated_at: null,
    ...over,
  };
}

describe("online attention", () => {
  it("is quiet for healthy enabled and paused configs, and for an empty list", () => {
    expect(onlineAttention([])).toEqual([]);
    expect(onlineAttention([cfg("a"), cfg("b", { execution_status: "DISABLED" })])).toEqual([]);
  });

  it("flags each failed status as act", () => {
    for (const status of ["CREATE_FAILED", "UPDATE_FAILED", "ERROR"]) {
      expect(onlineAttention([cfg("a", { status })])).toMatchObject([
        { key: "a:failed", kind: "failed", signal: "act", configId: "a" },
      ]);
    }
  });

  it("flags an enabled duplicate as wait, but not a paused one", () => {
    expect(onlineAttention([cfg("a", { duplicate_enabled: true })])).toMatchObject([
      { key: "a:duplicate", kind: "duplicate", signal: "wait", configId: "a" },
    ]);
    expect(onlineAttention([cfg("b", { duplicate_enabled: true, execution_status: "DISABLED" })])).toEqual([]);
  });

  it("flags each transient status as settling (wait)", () => {
    for (const status of ["CREATING", "UPDATING", "DELETING"]) {
      expect(onlineAttention([cfg("a", { status, execution_status: "DISABLED" })])).toMatchObject([
        { kind: "settling", signal: "wait" },
      ]);
    }
  });

  it("gives one reason per config: failed beats duplicate beats settling", () => {
    expect(onlineAttentionKind(cfg("a", { status: "UPDATE_FAILED", duplicate_enabled: true }))).toBe("failed");
    expect(onlineAttentionKind(cfg("b", { status: "UPDATING", duplicate_enabled: true }))).toBe("duplicate");
    expect(onlineAttentionKind(cfg("c", { status: "UPDATING", duplicate_enabled: true, execution_status: "DISABLED" }))).toBe(
      "settling",
    );
    expect(onlineAttention([cfg("d", { status: "ERROR", duplicate_enabled: true })])).toHaveLength(1);
  });

  it("orders failed, then duplicate, then settling, keeping list order within a kind", () => {
    const entries = onlineAttention([
      cfg("s1", { status: "CREATING" }),
      cfg("d1", { duplicate_enabled: true }),
      cfg("f1", { status: "ERROR" }),
      cfg("ok"),
      cfg("s2", { status: "DELETING" }),
      cfg("f2", { status: "CREATE_FAILED" }),
      cfg("d2", { duplicate_enabled: true }),
    ]);
    expect(entries.map((e) => e.configId)).toEqual(["f1", "f2", "d1", "d2", "s1", "s2"]);
  });
});

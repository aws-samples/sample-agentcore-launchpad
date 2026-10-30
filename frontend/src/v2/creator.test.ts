import { describe, expect, it } from "vitest";

import type { AgentInfo, OnlineEvalConfigRow } from "../lib/api";
import type { EvaluationRunInfo } from "../lib/evaluation";
import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { creatorLabel } from "./format";
import { taskFromOnline, taskFromRun } from "./tasks";

function run(over: Partial<EvaluationRunInfo> = {}): EvaluationRunInfo {
  return {
    id: "run1",
    agent_id: "a1",
    agent_name: "hr-bot",
    dataset_name: "golden",
    mode: "batch",
    evaluators: ["Builtin.Helpfulness"],
    status: "completed",
    queue_position: null,
    scores: [],
    insights: {},
    session_ids: [],
    error: null,
    created_at: "2026-10-11T08:00:00Z",
    ...over,
  } as EvaluationRunInfo;
}

function online(over: Partial<OnlineEvalConfigRow> = {}): OnlineEvalConfigRow {
  return {
    config_id: "cfg1",
    arn: null,
    name: "live",
    description: "",
    owner: "agent",
    status: "ACTIVE",
    execution_status: "ENABLED",
    failure_reason: null,
    agent_id: "a1",
    agent_name: "hr-bot",
    matched_agent: null,
    detailed: true,
    mode: "scores",
    evaluators: [],
    sampling_percentage: 10,
    session_timeout_minutes: null,
    filter_count: 0,
    filters: [],
    data_source: { log_groups: [], service_name: null },
    insights: [],
    clustering_frequencies: [],
    execution_role_arn: null,
    results_log_group: "",
    duplicate_enabled: false,
    created_at: "2026-10-11T08:00:00Z",
    updated_at: null,
    ...over,
  };
}

describe("creatorLabel", () => {
  it("shows the recorded username", () => {
    expect(creatorLabel({ created_by: "u_1", created_by_name: "alice" })).toBe("alice");
    // the config admin / auth-disabled operator carry a name but no users.id
    expect(creatorLabel({ created_by: null, created_by_name: "admin" })).toBe("admin");
  });

  it("falls back to a dash when the creator is unknown", () => {
    expect(creatorLabel({ created_by: null, created_by_name: null })).toBe("—");
    expect(creatorLabel({ created_by_name: "" })).toBe("—");
    expect(creatorLabel({})).toBe("—"); // older backend: field absent
    expect(creatorLabel(null)).toBe("—");
    expect(creatorLabel(undefined)).toBe("—");
  });

  it("never shows anything but the recorded creator for an agent", () => {
    // presets, discovered runtimes and unresolved legacy owners carry no creator
    const legacy: Pick<AgentInfo, "created_by" | "created_by_name"> = {
      created_by: null,
      created_by_name: null,
    };
    expect(creatorLabel(legacy)).toBe("—");
    expect(creatorLabel({ ...legacy, created_by: "u_1", created_by_name: "alice" })).toBe("alice");
  });
});

describe("V2Task creator", () => {
  it("maps a run's creator", () => {
    expect(taskFromRun(run({ created_by: "u_1", created_by_name: "alice" })).createdByName).toBe("alice");
    expect(taskFromRun(run()).createdByName).toBeNull();
    expect(taskFromRun(run({ created_by_name: null })).createdByName).toBeNull();
  });

  it("maps an online config's creator", () => {
    expect(taskFromOnline(online({ created_by: "u_2", created_by_name: "bob" })).createdByName).toBe("bob");
    expect(taskFromOnline(online()).createdByName).toBeNull();
    expect(taskFromOnline(online({ created_by_name: null })).createdByName).toBeNull();
  });
});

describe("v2.common.creator", () => {
  it("is labelled in both languages", () => {
    expect(en.v2.common.creator).toBe("Creator");
    expect(zh.v2.common.creator).toBe("创建者");
  });
});

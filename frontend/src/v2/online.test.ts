import { describe, expect, it } from "vitest";

import type { OnlineEvalConfigRow } from "../lib/api";
import { copyDraft, createBody, MAX_DESCRIPTION } from "./online";

function row(patch: Partial<OnlineEvalConfigRow> = {}): OnlineEvalConfigRow {
  return {
    config_id: "cfg-1",
    arn: null,
    name: "lp_shop_cs_scores",
    description: "Shop CS quality",
    owner: "agent",
    status: "ACTIVE",
    execution_status: "ENABLED",
    failure_reason: null,
    agent_id: "a1",
    agent_name: "shop-cs",
    matched_agent: null,
    detailed: true,
    mode: "scores",
    evaluators: ["Builtin.Helpfulness", "Builtin.GoalSuccessRate"],
    sampling_percentage: 25,
    session_timeout_minutes: 30,
    filter_count: 2,
    filters: [
      { key: "session.id", operator: "Contains", value: { stringValue: "vip" } },
      { key: "turns", operator: "GreaterThan", value: { doubleValue: 2 } },
    ],
    data_source: { log_groups: [], service_name: null },
    insights: [],
    clustering_frequencies: [],
    execution_role_arn: null,
    results_log_group: "/aws/bedrock-agentcore/evaluations/results/cfg-1",
    duplicate_enabled: false,
    created_at: null,
    updated_at: null,
    ...patch,
  };
}

describe("copyDraft (?view=new&from=online:<id>)", () => {
  it("copies the agent, evaluators, sampling, timeout and filters, suffixing the description", () => {
    const { agentId, draft } = copyDraft(row(), " copy");
    expect(agentId).toBe("a1");
    expect(draft.mode).toBe("scores");
    expect(draft.evaluators).toEqual(["Builtin.Helpfulness", "Builtin.GoalSuccessRate"]);
    expect(draft.sampling).toBe("25");
    expect(draft.timeout).toBe("30");
    expect(draft.filters).toEqual([
      { key: "session.id", operator: "Contains", kind: "string", value: "vip" },
      { key: "turns", operator: "GreaterThan", kind: "number", value: "2" },
    ]);
    expect(draft.description).toBe("Shop CS quality copy");
    // the copy creates the same rule on the same agent
    const body = createBody(agentId, draft, true);
    expect(body).toMatchObject({ agent_id: "a1", mode: "scores", sampling_percentage: 25, session_timeout_minutes: 30 });
    expect(body.filters).toEqual(row().filters);
  });

  it("copies an insights config's types and report frequencies", () => {
    const { draft } = copyDraft(
      row({
        mode: "insights",
        evaluators: [],
        insights: ["Builtin.Insight.FailureAnalysis"],
        clustering_frequencies: ["WEEKLY", "BOGUS"],
      }),
      " 副本",
    );
    expect(draft.mode).toBe("insights");
    expect(draft.insights).toEqual(["Builtin.Insight.FailureAnalysis"]);
    expect(draft.frequencies).toEqual(["WEEKLY"]);
    expect(draft.description).toBe("Shop CS quality 副本");
  });

  it("names the copy after the config when it has no description, and caps the length", () => {
    expect(copyDraft(row({ description: "  " }), " copy").draft.description).toBe("lp_shop_cs_scores copy");
    expect(copyDraft(row({ description: "", name: null }), " copy").draft.description).toBe("cfg-1 copy");
    const long = copyDraft(row({ description: "x".repeat(MAX_DESCRIPTION) }), " copy").draft.description;
    expect(long).toHaveLength(MAX_DESCRIPTION);
  });

  it("keeps the copied sampling when the mode is switched", () => {
    expect(copyDraft(row(), " copy").draft.samplingTouched).toBe(true);
  });

  it("leaves the agent empty for a config without one", () => {
    expect(copyDraft(row({ agent_id: null }), " copy").agentId).toBe("");
  });
});

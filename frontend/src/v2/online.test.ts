import i18next, { type TFunction } from "i18next";
import { beforeAll, describe, expect, it } from "vitest";

import type { OnlineEvalConfigRow, OnlineEvalParent } from "../lib/api";
import en from "../locales/en/common.json";
import zhCN from "../locales/zh-CN/common.json";
import { agentLabel, copyDraft, createBody, isLeftover, MAX_DESCRIPTION, ownerLabel, parentLink, parentNote } from "./online";

let tEn: TFunction;
let tZh: TFunction;

beforeAll(async () => {
  const make = async (lng: string) => {
    const inst = i18next.createInstance();
    await inst.init({
      lng,
      resources: { en: { common: en }, "zh-CN": { common: zhCN } },
      defaultNS: "common",
      interpolation: { escapeValue: false },
    });
    return inst.t;
  };
  tEn = await make("en");
  tZh = await make("zh-CN");
});

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
    parent: null,
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

function parent(patch: Partial<OnlineEvalParent> = {}): OnlineEvalParent {
  return { kind: "experiment", id: "abcdef12bbbb", name: "prompt v2", status: "running", agent_name: "hr-agent", arm: null, ...patch };
}

/** An experiment-owned config row (summary-only, no ledger agent). */
function owned(p: OnlineEvalParent | null): OnlineEvalConfigRow {
  return row({ owner: "experiment", agent_id: null, agent_name: null, name: "exp_abcdef12_oe1", parent: p });
}

describe("experiment-owned configs (parent)", () => {
  it("tags the owner by kind, the generic label only without a parent", () => {
    expect(ownerLabel(tZh, owned(parent()))).toBe("A/B 实验");
    expect(ownerLabel(tZh, owned(parent({ kind: "canary" })))).toBe("金丝雀");
    expect(ownerLabel(tEn, owned(parent({ kind: "canary" })))).toBe("Canary");
    expect(ownerLabel(tZh, owned(null))).toBe("A/B 实验 / 金丝雀");
    expect(ownerLabel(tZh, row())).toBe("Agent");
    expect(ownerLabel(tZh, row({ owner: "external" }))).toBe("外部");
  });

  it("falls back to the owning record's agent after the row's own and the external match", () => {
    expect(agentLabel(owned(parent()))).toBe("hr-agent");
    expect(agentLabel(owned(parent({ agent_name: null })))).toBe("—");
    expect(agentLabel(owned(null))).toBe("—");
    expect(agentLabel(row())).toBe("shop-cs");
    expect(agentLabel(row({ agent_name: null, matched_agent: { id: "a2", name: "other" } }))).toBe("≈ other");
  });

  it("links to the experiment / canary itself, else to its list tab", () => {
    expect(parentLink(owned(parent()))).toBe("/v2/eval/experiments?view=detail&id=abcdef12bbbb");
    expect(parentLink(owned(parent({ kind: "canary", id: "cafe0001cccc" })))).toBe(
      "/v2/eval/experiments?mode=canary&canary=cafe0001cccc",
    );
    expect(parentLink(owned(parent({ id: null, name: null })))).toBe("/v2/eval/experiments?mode=configuration");
    expect(parentLink(owned(parent({ kind: "canary", id: null, name: null })))).toBe("/v2/eval/experiments?mode=canary");
    expect(parentLink(owned(null))).toBe("/v2/eval/experiments?mode=configuration");
    expect(parentLink(row())).toBeNull();
    expect(parentLink(row({ owner: "external" }))).toBeNull();
  });

  it("calls a config of a cleaned or vanished owner a leftover", () => {
    expect(isLeftover(owned(parent()))).toBe(false);
    expect(isLeftover(owned(parent({ status: "cleaned" })))).toBe(true);
    expect(isLeftover(owned(parent({ id: null, name: null, status: null })))).toBe(true);
    expect(isLeftover(owned(null))).toBe(true);
    expect(isLeftover(row())).toBe(false);
    expect(isLeftover(row({ owner: "external" }))).toBe(false);
  });

  it("words the read-only alert by kind and arm, naming the record while it exists", () => {
    const text = (r: OnlineEvalConfigRow, t = tZh) => {
      const note = parentNote(r)!;
      return t(note.text, { name: note.name ?? "" });
    };
    expect(text(owned(parent()))).toBe("由 A/B 实验「prompt v2」自动创建，给实验两组流量打分；实验清理时会一并删除，这里只读。");
    expect(text(owned(parent({ kind: "canary", name: "store", arm: "control" })))).toContain("金丝雀「store」自动创建，给对照版本的流量打分");
    expect(text(owned(parent({ kind: "canary", name: "store", arm: "treatment" })))).toContain("给候选版本的流量打分");
    expect(text(owned(parent({ kind: "canary", name: "store", arm: null })))).toContain("给对照和候选版本的流量打分");
    expect(text(owned(parent({ id: null, name: null })))).toContain("由一个已删除的 A/B 实验自动创建");
    // a pre-split experiment's `_oe2` judged only its built-in canary's challenger
    expect(text(owned(parent({ arm: "treatment" })))).toContain("A/B 实验「prompt v2」自动创建，给实验候选版本的流量打分");
    expect(text(owned(parent({ arm: "treatment" })), tEn)).toContain("to score the traffic of its candidate version");
    expect(text(owned(parent({ kind: "canary", id: null, name: null, arm: "treatment" })))).toBe(
      "由一个已删除的金丝雀自动创建，给候选版本的流量打分；金丝雀清理时会一并删除，这里只读。",
    );
    expect(text(owned(parent({ kind: "canary", name: "store", arm: "control" })), tEn)).toBe(
      "Created by the canary “store” to score the traffic of the control version. The canary's cleanup deletes it; it is read-only here.",
    );
    // a record without a name still reads as existing, under its id
    expect(parentNote(owned(parent({ name: "" })))!.name).toBe("abcdef12bbbb");
  });

  it("labels the link and adds the leftover line only for a cleaned or vanished owner", () => {
    const live = parentNote(owned(parent()))!;
    expect(tZh(live.open)).toBe("打开该 A/B 实验");
    expect(live.leftover).toBeNull();
    const cleaned = parentNote(owned(parent({ kind: "canary", status: "cleaned" })))!;
    expect(tZh(cleaned.open)).toBe("打开该金丝雀");
    expect(tZh(cleaned.leftover!)).toBe("所属的金丝雀已清理或已不存在，但这条配置当时没有被删掉，是遗留下来的。");
    const gone = parentNote(owned(parent({ id: null, name: null })))!;
    expect(tZh(gone.open)).toBe("打开 A/B 实验列表");
    expect(tEn(gone.leftover!)).toMatch(/^The A\/B test it belongs to has been cleaned up/);
    expect(parentNote(row())).toBeNull();
    expect(parentNote(row({ owner: "external" }))).toBeNull();
  });
});

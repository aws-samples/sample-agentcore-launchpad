import { describe, expect, it } from "vitest";

import type { AssistantProposal } from "./api";
import {
  PROPOSAL_CARRIED_KEYS,
  PROPOSAL_EDITED_KEYS,
  proposalContentFromDraft,
  proposalDraftFrom,
} from "./assistant";

/**
 * N15-obs13: a member 编辑 of an architect proposal rebuilt the content from the
 * editable draft and enumerated the carried solution members by hand — `evaluation_plan`
 * (seed evaluators, `recommendation_keys`, scenarios, blocked tests) was dropped on every
 * edit, so the drafted review plan had no seed evaluators and a blocking recommendation
 * naming one became an unresolvable gate. Every contract key the editor does not expose
 * must travel with the revision verbatim.
 */

const SEED = {
  evaluators: [{
    kind: "judge", key: "hr-guardrails-judge", title: "HR guardrails judge",
    name: "hr_guardrails_judge", level: "TRACE",
    instructions: "Judge {context} against the HR guardrails", golden_test_ids: ["GT-1"],
  }],
  scenarios: [{
    scenario_id: "GT-1", golden_test_id: "GT-1",
    turns: [{ input: "How many vacation days do I get?" }], assertions: ["cites the leave policy"],
  }],
  recommendation_keys: { "0": ["hr-guardrails-judge"] },
  blocked_golden_tests: [{ golden_test_id: "GT-2", reason: "needs the HRIS write API" }],
};

/** A model-authored revision 1 as the API returns it (every optional member present). */
const MODEL_CONTENT: AssistantProposal["content"] = {
  version: 1,
  name: "hr-helpdesk",
  model_id: "global.zai.glm-5.3",
  model_source: "bedrock",
  system_prompt: "You answer HR policy questions.",
  tools: ["gateway:hr-tools", "mcp:deepwiki"],
  tool_functions: { "gateway:hr-tools": ["hr-tools___get_employee"] },
  native_tools: ["shell"],
  builtin_tools: ["code-interpreter"],
  skills: ["meeting-summarizer"],
  knowledge_bases: ["KB123ABC"],
  memory: "workspace",
  max_iterations: 12,
  timeout_seconds: 240,
  summary: "An HR helpdesk agent.",
  requirements_baseline: ["answers policy questions"],
  assumptions: ["policies are in the KB"],
  manual_tasks: ["connect the HRIS write API"],
  golden_tests: [
    { id: "GT-1", input: "How many vacation days do I get?", pass_criteria: "cites the leave policy", source: "customer_pain_point" },
    { id: "GT-2", input: "Update my address", pass_criteria: "refuses: no write API", source: "customer_pain_point" },
  ],
  evaluator_recommendations: [
    { text: "hr_guardrails_judge must block publishing at threshold 1.00", evaluator_id: "hr_guardrails_judge", blocking: true, threshold: 1.0 },
  ],
  evaluation_plan: SEED,
  fishbone: {
    use_case: "HR helpdesk", customer: "ACME", date: "2026-10-19", scenario: "internal",
    coverage: { cognition: "confirmed", quality: "confirmed", responsibility: "explored_empty", cost: "explored_empty", performance: "explored_empty", other: "explored_empty" },
    barriers: { cognition: [{ sticky_text: "answers drift", evidence: "", customer_quote: "", confirmed: true, selected: true }] },
    parking_lot: [],
  } as unknown as AssistantProposal["content"]["fishbone"],
};

const canonical = (v: unknown) => JSON.stringify(v);

describe("proposalContentFromDraft — N15-obs13", () => {
  it("the edited and carried key lists together cover the whole proposal contract", () => {
    // `version` is always written; `tool_functions` is carried through its own narrowing.
    const covered = new Set<string>([...PROPOSAL_EDITED_KEYS, ...PROPOSAL_CARRIED_KEYS, "version", "tool_functions"]);
    expect([...Object.keys(MODEL_CONTENT)].filter((k) => !covered.has(k))).toEqual([]);
    expect(PROPOSAL_EDITED_KEYS.filter((k) => (PROPOSAL_CARRIED_KEYS as readonly string[]).includes(k))).toEqual([]);
  });

  it("round-trips a model-authored proposal: evaluation_plan survives byte-identically", () => {
    const draft = proposalDraftFrom(MODEL_CONTENT);
    const out = proposalContentFromDraft(draft, MODEL_CONTENT);
    expect(canonical(out.evaluation_plan)).toBe(canonical(SEED));
    // seed evaluators, recommendation_keys, scenarios and blocked tests all intact
    expect(out.evaluation_plan).toEqual(SEED);
  });

  it("an unchanged edit reproduces every contract key of the base", () => {
    const draft = proposalDraftFrom(MODEL_CONTENT);
    const out = proposalContentFromDraft(draft, MODEL_CONTENT) as unknown as Record<string, unknown>;
    for (const key of Object.keys(MODEL_CONTENT)) {
      expect(canonical(out[key]), key).toBe(canonical(MODEL_CONTENT[key]));
    }
    expect(Object.keys(out).sort()).toEqual(Object.keys(MODEL_CONTENT).sort());
  });

  it("an optional key the base does not carry stays absent (hash stability)", () => {
    const base = Object.fromEntries(Object.entries(MODEL_CONTENT).filter(
      ([k]) => !["evaluation_plan", "fishbone", "tool_functions"].includes(k),
    )) as AssistantProposal["content"];
    const out = proposalContentFromDraft(proposalDraftFrom(base), base);
    expect("evaluation_plan" in out).toBe(false);
    expect("fishbone" in out).toBe(false);
    expect("tool_functions" in out).toBe(false);
  });

  it("an edited field replaces the base's while the base's golden tests and seed travel on", () => {
    const draft = {
      ...proposalDraftFrom(MODEL_CONTENT),
      name: "hr-helpdesk-v2",
      system_prompt: "You answer HR policy questions. Cite the policy.",
      max_iterations: 20,
      memory: "disabled" as const,
    };
    const out = proposalContentFromDraft(draft, MODEL_CONTENT);
    expect(out.name).toBe("hr-helpdesk-v2");
    expect(out.system_prompt).toBe("You answer HR policy questions. Cite the policy.");
    expect(out.max_iterations).toBe(20);
    expect(out.memory).toBe("disabled");
    expect(out.golden_tests).toEqual(MODEL_CONTENT.golden_tests);
    expect(out.evaluator_recommendations).toEqual(MODEL_CONTENT.evaluator_recommendations);
    expect(out.evaluation_plan).toEqual(SEED);
    expect(out.builtin_tools).toEqual(["code-interpreter"]);
  });

  it("a function narrowing follows its Gateway: kept while selected, dropped when deselected", () => {
    const kept = proposalContentFromDraft(proposalDraftFrom(MODEL_CONTENT), MODEL_CONTENT);
    expect(kept.tool_functions).toEqual({ "gateway:hr-tools": ["hr-tools___get_employee"] });
    const draft = { ...proposalDraftFrom(MODEL_CONTENT), tools: ["mcp:deepwiki"] };
    const out = proposalContentFromDraft(draft, MODEL_CONTENT);
    expect(out.tools).toEqual(["mcp:deepwiki"]);
    expect("tool_functions" in out).toBe(false);
  });

  it("keys outside the contract (an invalid revision's junk) are not carried", () => {
    const base = { ...MODEL_CONTENT, code: "print(1)", env: { X: "1" } };
    const out = proposalContentFromDraft(proposalDraftFrom(base), base) as unknown as Record<string, unknown>;
    expect("code" in out).toBe(false);
    expect("env" in out).toBe(false);
    expect(out.evaluation_plan).toEqual(SEED);
  });
});

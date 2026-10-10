import { describe, expect, it } from "vitest";

import {
  emptyScenario,
  isMixedSimulated,
  parseDatasetImport,
  SAMPLE_SCENARIOS,
  ScenarioJsonError,
  SIM_SAMPLE_SCENARIOS,
  toDrafts,
  toItems,
  toSimDrafts,
  toSimItems,
} from "./datasetDrafts";

describe("predefined scenario drafts", () => {
  it("round-trips a multi-turn scenario with assertions, trajectory and extra keys", () => {
    const item = {
      scenario_id: "s1",
      turns: [{ input: "hi", expected_response: "hello" }, { input: "bye" }],
      assertions: ["greets back"],
      expected_trajectory: ["calculator", "current_time"],
      metadata: { source: "trace" },
    };
    expect(toItems(toDrafts([item]), "predefined")).toEqual([item]);
  });

  it("keeps a legacy dataset in its prompt shape while the content fits it", () => {
    const items = [{ prompt: "2+2?", expected: "4", note: "kept" }];
    expect(toItems(toDrafts(items), "legacy")).toEqual(items);
  });

  it("emits a legacy dataset as scenarios once a scenario gains a second turn", () => {
    const drafts = toDrafts([{ prompt: "2+2?" }]);
    drafts[0].turns.push({ input: "and 3+3?", expected_response: "" });
    const [item] = toItems(drafts, "legacy");
    expect(item.turns).toEqual([{ input: "2+2?" }, { input: "and 3+3?" }]);
  });

  it("holds an item the form cannot represent as JSON-only and saves it verbatim", () => {
    const item = { scenario_id: "s", turns: [{ input: "x", metadata: { a: 1 } }] };
    const [draft] = toDrafts([item]);
    expect(draft.jsonOnly).toBe("turnKeys");
    expect(toItems([draft], "predefined")).toEqual([item]);
  });

  it("reports the 1-based scenario index of invalid JSON", () => {
    const drafts = [emptyScenario(1), { ...emptyScenario(2), json: "{nope" }];
    expect(() => toItems(drafts, "predefined")).toThrow(ScenarioJsonError);
    try {
      toItems(drafts, "predefined");
    } catch (err) {
      expect((err as ScenarioJsonError).index).toBe(2);
    }
  });

  it("ships a sample whose scenarios all carry input", () => {
    for (const item of toItems(SAMPLE_SCENARIOS(), "predefined")) {
      expect((item.turns as { input: string }[]).every((turn) => turn.input.trim())).toBe(true);
    }
  });
});

describe("simulated persona drafts", () => {
  it("round-trips a persona, keeping unknown item and profile keys", () => {
    const item = {
      scenario_id: "p1",
      scenario_description: "desc",
      actor_profile: { context: "ctx", goal: "goal", traits: { tone: "calm" }, locale: "en" },
      input: "hello",
      max_turns: 6,
      assertions: ["verifies first"],
      metadata: { from: "import" },
    };
    expect(toSimItems(toSimDrafts([item]))).toEqual([item]);
  });

  it("omits the default max_turns and empty optional fields", () => {
    const [item] = toSimItems([
      { scenario_id: "p", scenario_description: " ", context: "c", goal: "g", traits: [{ key: " ", value: "x" }], input: "i", max_turns: "10", assertions: [" "] },
    ]);
    expect(item).toEqual({ scenario_id: "p", actor_profile: { context: "c", goal: "g" }, input: "i" });
  });

  it("ships a sample whose personas all carry context, goal and input", () => {
    for (const item of toSimItems(SIM_SAMPLE_SCENARIOS())) {
      const profile = item.actor_profile as { context: string; goal: string };
      expect(profile.context && profile.goal && item.input).toBeTruthy();
    }
  });

  it("flags a simulated dataset that holds non-persona items", () => {
    expect(isMixedSimulated("simulated", [{ actor_profile: {} }, { prompt: "x" }])).toBe(true);
    expect(isMixedSimulated("simulated", [{ actor_profile: {} }])).toBe(false);
    expect(isMixedSimulated("predefined", [{ prompt: "x" }])).toBe(false);
  });
});

describe("parseDatasetImport", () => {
  it("accepts {scenarios}, a bare array, one object and JSONL", () => {
    expect(parseDatasetImport('{"scenarios": [{"a": 1}, {"a": 2}]}').items).toHaveLength(2);
    expect(parseDatasetImport('[{"a": 1}]').items).toHaveLength(1);
    expect(parseDatasetImport('{"prompt": "x"}').items).toEqual([{ prompt: "x" }]);
    expect(parseDatasetImport('{"prompt": "x"}\n\n{"prompt": "y"}').items).toHaveLength(2);
  });

  it("names the failing JSONL line and rejects a non-array scenarios key", () => {
    expect(parseDatasetImport('{"prompt": "x"}\n{bad').error).toEqual({ code: "badLine", line: 2 });
    expect(parseDatasetImport('{"scenarios": {}}').error).toEqual({ code: "noScenarios" });
    expect(parseDatasetImport("   ")).toEqual({ items: [], error: null });
  });
});

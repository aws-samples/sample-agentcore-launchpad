import { describe, expect, it } from "vitest";

import { emptyScenario, fromDraft, fromDrafts, toDraft, toolNames, viewOf } from "./scenarioDraft";

// N17-obs424: the dataset editor's scenario model round-trips every field the backend reads
describe("scenarioDraft", () => {
  const stored = {
    scenario_id: "refund-1",
    turns: [
      { input: "Where is my order?", expected_response: "It ships today.", note: "keep me" },
      { input: { content: "And the refund?" } },
    ],
    assertions: ["mentions the order", "offers a refund"],
    expected_trajectory: ["launchpad_gw_orders___lookup", "refund"],
    split: "held_out",
    metadata: { launchpad_assets: { golden_test_id: "G1" } },
  };

  it("reads a stored scenario", () => {
    const d = toDraft(stored, 0);
    expect(d.scenarioId).toBe("refund-1");
    expect(d.turns.map((t) => [t.input, t.expected])).toEqual([["Where is my order?", "It ships today."], ["And the refund?", ""]]);
    expect(d.assertions).toBe("mentions the order\noffers a refund");
    expect(d.trajectoryMode).toBe("list");
    expect(d.split).toBe("held_out");
  });

  it("writes it back unchanged, keeping keys the editor does not own", () => {
    const out = fromDraft(toDraft(stored, 0), "predefined");
    expect(out.metadata).toEqual(stored.metadata);
    expect((out.turns as Record<string, unknown>[])[0]).toEqual(stored.turns[0]);
    expect(out.assertions).toEqual(stored.assertions);
    expect(out.expected_trajectory).toEqual(stored.expected_trajectory);
    expect(out.split).toBe("held_out");
  });

  it("separates no expectation from an empty trajectory", () => {
    const d = { ...toDraft(stored, 0), trajectoryMode: "list" as const, trajectory: "" };
    expect(fromDraft(d, "predefined").expected_trajectory).toEqual([]);
    const none = fromDraft({ ...d, trajectoryMode: "none" }, "predefined");
    expect("expected_trajectory" in none).toBe(false);
  });

  it("drops cleared fields and empty turns / scenarios", () => {
    const d = { ...toDraft(stored, 0), assertions: " \n", split: "" as const };
    d.turns = [...d.turns, { input: "  ", expected: "x", original: null }];
    const out = fromDraft(d, "predefined");
    expect("assertions" in out).toBe(false);
    expect("split" in out).toBe(false);
    expect((out.turns as unknown[]).length).toBe(2);
    expect(fromDrafts([emptyScenario(1), d], "predefined")).toHaveLength(1);
  });

  it("keeps a legacy prompt item's shape", () => {
    const d = toDraft({ prompt: "hi", expected: "hello" }, 2);
    expect(d.scenarioId).toBe("item_3");
    const out = fromDraft({ ...d, split: "tuning" }, "legacy");
    expect(out).toEqual({ prompt: "hi", expected: "hello", split: "tuning" });
  });

  it("parses tool names and builds the read-only view", () => {
    expect(toolNames("a, b\nc,, ")).toEqual(["a", "b", "c"]);
    const v = viewOf(stored, 0);
    expect(v.trajectory).toEqual(stored.expected_trajectory);
    expect(v.assertions).toHaveLength(2);
    expect(viewOf({ scenario_id: "p", actor_profile: { context: "c", goal: "g" }, input: "hey" }, 0).turns).toEqual([{ input: "hey", expected: "" }]);
  });
});

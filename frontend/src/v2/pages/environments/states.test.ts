import { describe, expect, it } from "vitest";

import type { EnvironmentComparison, EnvironmentRow } from "../../../lib/api";
import { changedGroupKeys, COMPARE_SIGNAL, compareStateKey, compareSummary } from "./states";

function row(over: Partial<EnvironmentRow> = {}): EnvironmentRow {
  return {
    workspace: { id: "default", name: "Default", account_id: "111122223333", region: "us-west-2", status: "ready" },
    baseline: false,
    present: true,
    state: "same",
    changed_groups: [],
    agent: null,
    ...over,
  };
}

function result(rows: EnvironmentRow[], summary: Partial<EnvironmentComparison["summary"]> = {}): EnvironmentComparison {
  return {
    agent: "hr-assistant",
    baseline_workspace: "default",
    environments: rows,
    summary: {
      workspaces: rows.length,
      present: rows.filter((r) => r.present).length,
      same: 0,
      differs: 0,
      absent: 0,
      distinct_spec_digests: 1,
      aligned: false,
      ...summary,
    },
  };
}

describe("signals", () => {
  it("maps every compare state", () => {
    expect(COMPARE_SIGNAL).toEqual({ baseline: "info", same: "ok", differs: "wait", absent: "off", no_baseline: "off" });
    expect(compareStateKey("no_baseline")).toBe("v2.environments.compareState.no_baseline");
  });

  it("labels differing groups, image included", () => {
    expect(changedGroupKeys(["prompt", "image", "x"])).toEqual([
      "v2.agents.snapshots.group.prompt",
      "v2.agents.snapshots.group.image",
      "v2.agents.snapshots.group.other",
    ]);
  });
});

describe("compareSummary", () => {
  it("nowhere when no workspace runs the agent", () => {
    expect(compareSummary(result([row({ baseline: true, present: false, state: "absent" })])).key).toBe(
      "v2.environments.nowhere",
    );
  });

  it("no baseline when the current workspace lacks the agent", () => {
    const summary = compareSummary(
      result([row({ baseline: true, present: false, state: "absent" }), row({ state: "no_baseline" })]),
    );
    expect(summary).toMatchObject({ tone: "warn", key: "v2.environments.noBaseline" });
  });

  it("aligned when every present row matches the baseline", () => {
    const summary = compareSummary(
      result([row({ baseline: true, state: "baseline" }), row({ state: "same" })], { aligned: true, same: 1 }),
    );
    expect(summary).toMatchObject({ tone: "success", key: "v2.environments.aligned" });
  });

  it("not aligned names how many differ", () => {
    const summary = compareSummary(
      result([row({ baseline: true, state: "baseline" }), row({ state: "differs" })], { differs: 1 }),
    );
    expect(summary).toEqual({
      tone: "warn",
      key: "v2.environments.notAligned",
      params: { agent: "hr-assistant", differs: 1 },
    });
  });
});

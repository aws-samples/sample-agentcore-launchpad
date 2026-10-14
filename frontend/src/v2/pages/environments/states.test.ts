import { describe, expect, it } from "vitest";

import type { EnvironmentComparison, EnvironmentRow } from "../../../lib/api";
import {
  changedGroupKeys,
  COMPARE_SIGNAL,
  compareStateKey,
  compareSummary,
  DRIFT_SIGNAL,
  driftDetail,
  driftStateKey,
  rankDrift,
} from "./states";

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

  it("never paints unknown drift green", () => {
    expect(DRIFT_SIGNAL).toEqual({ in_sync: "ok", drift: "act", unknown: "wait" });
    expect(DRIFT_SIGNAL.unknown).not.toBe(DRIFT_SIGNAL.in_sync);
    expect(driftStateKey("in_sync")).toBe("v2.environments.state.in_sync");
  });

  it("labels differing groups, image included", () => {
    expect(changedGroupKeys(["prompt", "image", "x"])).toEqual([
      "v2.agents.snapshots.group.prompt",
      "v2.agents.snapshots.group.image",
      "v2.agents.snapshots.group.other",
    ]);
  });
});

describe("driftDetail", () => {
  it("lists every finding with its expected / observed", () => {
    expect(
      driftDetail({
        findings: [
          { code: "unhealthy", expected: "READY", observed: "FAILED" },
          { code: "version_changed", expected: "3", observed: "4" },
        ],
        reason: null,
      }),
    ).toEqual([
      { key: "v2.environments.finding.unhealthy", params: { expected: "READY", observed: "FAILED" } },
      { key: "v2.environments.finding.version_changed", params: { expected: "3", observed: "4" } },
    ]);
  });

  it("a missing resource has no observed value", () => {
    expect(driftDetail({ findings: [{ code: "resource_missing", expected: "rt-1", observed: null }], reason: null })).toEqual([
      { key: "v2.environments.finding.resource_missing", params: { expected: "rt-1", observed: "—" } },
    ]);
  });

  it("an unknown row says why; an in-sync row says nothing", () => {
    expect(driftDetail({ findings: [], reason: "canary_active" })).toEqual([
      { key: "v2.environments.reason.canary_active" },
    ]);
    expect(driftDetail({ findings: [], reason: null })).toEqual([]);
  });
});

describe("rankDrift", () => {
  it("puts drift first, then unknown, keeping the name order inside a state", () => {
    const ranked = rankDrift([
      { name: "a", state: "in_sync" as const },
      { name: "b", state: "unknown" as const },
      { name: "c", state: "drift" as const },
      { name: "d", state: "drift" as const },
      { name: "e", state: "in_sync" as const },
    ]);
    expect(ranked.map((r) => r.name)).toEqual(["c", "d", "b", "a", "e"]);
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

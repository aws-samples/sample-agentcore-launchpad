import { describe, expect, it } from "vitest";

import type { RuntimeCanaryInfo } from "../../../lib/api";
import type { ExperimentInfo } from "../../../lib/experiments";
import { canaryKind, canaryQueue, experimentKind, experimentQueue } from "./attention";

const exp = (id: string, over: Partial<ExperimentInfo> = {}) =>
  ({
    id,
    name: `exp-${id}`,
    agent_id: "a1",
    agent_name: "agent-1",
    status: "running",
    stage: "recommend",
    stages: [],
    running_action: null,
    progress: null,
    error: null,
    created_at: "2026-10-12T08:00:00",
    artifacts: {},
    ...over,
  }) as ExperimentInfo;

const can = (id: string, over: Partial<RuntimeCanaryInfo> = {}) =>
  ({
    id,
    name: `canary-${id}`,
    champion_agent_id: "a1",
    champion_agent_name: "agent-1",
    challenger_agent_id: "a1",
    challenger_agent_name: "agent-1",
    source_experiment_id: null,
    status: "running",
    stage: "traffic",
    stages: [],
    running_action: null,
    progress: null,
    error: null,
    created_at: "2026-10-12T08:00:00",
    artifacts: {},
    ...over,
  }) as RuntimeCanaryInfo;

describe("experiment queue", () => {
  it("is empty for no experiments and for settled ones", () => {
    expect(experimentQueue([])).toEqual([]);
    const settled = ["promoted", "failed", "cleaned"].map((status, i) => exp(`e${i}`, { status }));
    expect(experimentQueue(settled)).toEqual([]);
  });

  it("queues a running experiment as info and a ready one as wait", () => {
    expect(experimentQueue([exp("a")])).toMatchObject([{ key: "a:moving", kind: "moving", signal: "info" }]);
    expect(experimentQueue([exp("b", { status: "ready" })])).toMatchObject([{ key: "b:decide", kind: "decide", signal: "wait" }]);
  });

  it("treats an action in flight as moving whatever the status", () => {
    expect(experimentKind({ status: "ready", running_action: "promote" })).toBe("moving");
    expect(experimentKind({ status: "promoted", running_action: "cleanup" })).toBe("moving");
    expect(experimentKind({ status: "failed", running_action: null })).toBeNull();
  });

  it("puts a decision before a moving stage and keeps list order otherwise", () => {
    const entries = experimentQueue([
      exp("m1"),
      exp("r1", { status: "ready" }),
      exp("done", { status: "promoted" }),
      exp("m2", { status: "ready", running_action: "promote" }),
      exp("r2", { status: "ready" }),
    ]);
    expect(entries.map((e) => e.experiment.id)).toEqual(["r1", "r2", "m1", "m2"]);
  });
});

describe("canary queue", () => {
  it("is empty for no canaries and for finished ones", () => {
    expect(canaryQueue([])).toEqual([]);
    const done = (["completed", "rolled_back", "cleaned"] as const).map((status, i) => can(`c${i}`, { status, stage: "verdict" }));
    expect(canaryQueue(done)).toEqual([]);
  });

  it("makes the verdict stage wait and any other running stage info", () => {
    expect(canaryQueue([can("v", { stage: "verdict" })])).toMatchObject([{ kind: "decide", signal: "wait" }]);
    for (const stage of ["created", "setup", "traffic", "ramp"]) {
      expect(canaryQueue([can("m", { stage })])).toMatchObject([{ kind: "moving", signal: "info" }]);
    }
  });

  it("treats an action in flight as moving, even at the verdict stage", () => {
    expect(canaryKind({ status: "running", stage: "verdict", running_action: "advance" })).toBe("moving");
    expect(canaryKind({ status: "completed", stage: "complete", running_action: "cleanup" })).toBe("moving");
  });

  it("puts a decision before a moving stage and keeps list order otherwise", () => {
    const entries = canaryQueue([
      can("m1"),
      can("v1", { stage: "verdict" }),
      can("x", { status: "cleaned" }),
      can("v2", { stage: "verdict" }),
      can("m2", { stage: "setup" }),
    ]);
    expect(entries.map((e) => e.canary.id)).toEqual(["v1", "v2", "m1", "m2"]);
  });
});

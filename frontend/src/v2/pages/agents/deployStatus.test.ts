import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { DeploymentInfo, StageInfo } from "../../../lib/api";
import { deployLine, fmtElapsed, isFailed, progressKey, runningStage } from "./deployStatus";

const STAGES = ["generate", "package", "provision", "deploy", "register"];

function deployment(
  statuses: StageInfo["status"][],
  extra: Partial<DeploymentInfo> = {},
): DeploymentInfo {
  return {
    id: "d1",
    agent_id: "a1",
    job_id: "j1",
    status: "running",
    stages: STAGES.map((name, i) => ({ name, status: statuses[i] ?? "pending", detail: "" })),
    started_at: "2026-10-13T08:00:00.000000",
    ended_at: null,
    ...extra,
  };
}

const stage = (name: string): StageInfo => ({ name, status: "running", detail: "" });

describe("progressKey", () => {
  it("uses one line for every Harness stage", () => {
    for (const name of STAGES) expect(progressKey("harness", stage(name))).toBe("harness");
  });

  it("names the package step per method", () => {
    expect(progressKey("zip_runtime", stage("package"))).toBe("zipPackage");
    expect(progressKey("container", stage("package"))).toBe("containerBuild");
    expect(progressKey("studio", stage("package"))).toBe("containerBuild");
    expect(progressKey("byoc", stage("package"))).toBe("package");
  });

  it("maps generate, register and the rest", () => {
    expect(progressKey("container", stage("generate"))).toBe("prepare");
    expect(progressKey("container", stage("register"))).toBe("register");
    expect(progressKey("container", stage("provision"))).toBe("start");
    expect(progressKey("container", stage("deploy"))).toBe("start");
    expect(progressKey("container", undefined)).toBe("start");
  });
});

describe("runningStage", () => {
  it("prefers the running stage, then the first pending one", () => {
    expect(runningStage(deployment(["succeeded", "running"]))?.name).toBe("package");
    expect(runningStage(deployment(["succeeded", "succeeded"]))?.name).toBe("provision");
    expect(runningStage(deployment(["succeeded", "succeeded", "succeeded", "succeeded", "succeeded"]))).toBeUndefined();
  });
});

describe("fmtElapsed", () => {
  it("formats seconds, then minutes with padded seconds", () => {
    expect(fmtElapsed(0)).toBe("0s");
    expect(fmtElapsed(59_999)).toBe("59s");
    expect(fmtElapsed(60_000)).toBe("1m 00s");
    expect(fmtElapsed(130_000)).toBe("2m 10s");
    expect(fmtElapsed(3_725_000)).toBe("62m 05s");
  });

  it("clamps a negative span (clock skew) to zero", () => {
    expect(fmtElapsed(-5_000)).toBe("0s");
  });
});

describe("deployLine", () => {
  // a naive ledger time must read as UTC: pin a non-UTC zone so a local-time parse
  // would shift the elapsed time by 8 hours instead of passing unseen
  const hostTz = process.env.TZ;
  beforeAll(() => {
    process.env.TZ = "Asia/Shanghai";
  });
  afterAll(() => {
    if (hostTz === undefined) delete process.env.TZ;
    else process.env.TZ = hostTz;
  });

  const START = Date.UTC(2026, 9, 13, 8, 0, 0);

  it("says nothing without a deployment", () => {
    expect(deployLine("container", null, START)).toEqual({ kind: "none" });
    expect(deployLine("container", undefined, START)).toEqual({ kind: "none" });
  });

  it("phrases the running stage with the time since a naive UTC start", () => {
    const d = deployment(["succeeded", "running"]);
    expect(deployLine("container", d, START + 130_000)).toEqual({
      kind: "running",
      key: "containerBuild",
      elapsedMs: 130_000,
    });
    expect(deployLine("zip_runtime", d, START + 5_000)).toMatchObject({ key: "zipPackage", elapsedMs: 5_000 });
    expect(deployLine("harness", d, START)).toMatchObject({ key: "harness" });
  });

  it("keeps a zoned start as is", () => {
    const d = deployment(["running"], { started_at: "2026-10-13T16:00:00+08:00" });
    expect(deployLine("container", d, START + 1_000)).toMatchObject({ kind: "running", key: "prepare", elapsedMs: 1_000 });
  });

  it("has no elapsed time when the start is missing or invalid", () => {
    expect(deployLine("container", deployment(["running"], { started_at: null }), START)).toMatchObject({
      kind: "running",
      elapsedMs: null,
    });
    expect(deployLine("container", deployment(["running"], { started_at: "garbage" }), START)).toMatchObject({
      elapsedMs: null,
    });
  });

  it("reports the total time of a finished deploy, not the time since", () => {
    const d = deployment(["succeeded", "succeeded", "succeeded", "succeeded", "succeeded"], {
      status: "succeeded",
      ended_at: "2026-10-13T08:02:10.000000",
    });
    expect(deployLine("container", d, START + 86_400_000)).toEqual({ kind: "done", elapsedMs: 130_000 });
  });

  it("names the stage that failed", () => {
    const d = deployment(["succeeded", "succeeded", "failed"], { status: "failed" });
    expect(isFailed(d)).toBe(true);
    expect(deployLine("container", d, START)).toEqual({ kind: "failed", stage: "provision" });
  });

  it("treats a failed stage as failed even while the deployment still says running", () => {
    const d = deployment(["succeeded", "failed"]);
    expect(isFailed(d)).toBe(true);
    expect(deployLine("zip_runtime", d, START)).toEqual({ kind: "failed", stage: "package" });
  });

  it("fails without a stage name when no stage is marked failed", () => {
    const d = deployment(["succeeded", "succeeded"], { status: "failed" });
    expect(deployLine("container", d, START)).toEqual({ kind: "failed", stage: null });
  });

  it("is not failed while every stage is fine", () => {
    expect(isFailed(deployment(["succeeded", "running"]))).toBe(false);
  });
});

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { V2Task } from "../../tasks";
import { failedTasks, HINT_MAX, TASK_ATTENTION_CAP } from "./attention";

function task(id: string, over: Partial<V2Task> = {}): V2Task {
  return {
    kind: "run",
    id,
    name: `task-${id}`,
    description: "",
    agentId: "a1",
    agentName: "agent-1",
    logSource: null,
    mode: "evaluators",
    evaluators: [],
    source: "dataset",
    sourceDetail: "",
    status: "failed",
    createdAt: "2026-10-12T08:00:00",
    updatedAt: "2026-10-12T08:00:00",
    createdByName: null,
    ...over,
  };
}

describe("failed task queue", () => {
  // run in UTC+8 whatever the host zone: in UTC a naive string reads the same either way
  const hostTz = process.env.TZ;
  beforeAll(() => {
    process.env.TZ = "Asia/Shanghai";
  });
  afterAll(() => {
    if (hostTz === undefined) delete process.env.TZ;
    else process.env.TZ = hostTz;
  });

  it("is empty when nothing failed", () => {
    expect(failedTasks([])).toEqual({ entries: [], total: 0 });
    const fine = (["queued", "running", "completed", "stopped", "paused", "pending"] as const).map((status, i) =>
      task(`t${i}`, { status }),
    );
    expect(failedTasks(fine)).toEqual({ entries: [], total: 0 });
  });

  it("queues failed runs and online tasks as act, keyed by kind", () => {
    const { entries } = failedTasks([task("r"), task("o", { kind: "online" }), task("ok", { status: "completed" })]);
    expect(entries.map((e) => [e.key, e.signal])).toEqual([
      ["run:r", "act"],
      ["online:o", "act"],
    ]);
  });

  it("orders newest first across naive and zoned timestamps", () => {
    const { entries } = failedTasks([
      task("old", { createdAt: "2026-10-10T08:00:00" }),
      // 2026-10-12 07:00 UTC — earlier than the naive 08:00 UTC below
      task("zoned", { createdAt: "2026-10-12T15:00:00+08:00" }),
      task("new", { createdAt: "2026-10-12T08:00:00" }),
      task("none", { createdAt: null }),
    ]);
    expect(entries.map((e) => e.task.id)).toEqual(["new", "zoned", "old", "none"]);
  });

  it(`caps the rows at ${TASK_ATTENTION_CAP} while the total counts every failure`, () => {
    const many = Array.from({ length: 8 }, (_, i) => task(`t${i}`, { createdAt: `2026-10-0${i + 1}T00:00:00` }));
    const { entries, total } = failedTasks(many);
    expect(total).toBe(8);
    expect(entries.map((e) => e.task.id)).toEqual(["t7", "t6", "t5", "t4", "t3"]);
    expect(failedTasks(many.slice(0, TASK_ATTENTION_CAP)).entries).toHaveLength(TASK_ATTENTION_CAP);
  });

  it("shows the first line of the error, clipped, falling back to the online failure reason", () => {
    const run = task("r", { run: { error: "  Boom: judge failed\nTraceback …" } as V2Task["run"] });
    const online = task("o", { kind: "online", online: { failure_reason: "role missing" } as V2Task["online"] });
    const bare = task("b");
    const long = task("l", { run: { error: "x".repeat(300) } as V2Task["run"] });
    const exact = task("e", { run: { error: "y".repeat(HINT_MAX) } as V2Task["run"] });
    const detail = Object.fromEntries(failedTasks([run, online, bare, long, exact]).entries.map((e) => [e.task.id, e.detail]));
    expect(detail.r).toBe("Boom: judge failed");
    expect(detail.o).toBe("role missing");
    expect(detail.b).toBe("");
    expect(detail.l).toHaveLength(HINT_MAX);
    expect(detail.l.endsWith("…")).toBe(true);
    expect(detail.e).toBe("y".repeat(HINT_MAX));
  });
});

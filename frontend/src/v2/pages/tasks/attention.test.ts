import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { V2Task } from "../../tasks";
import { FAILED_RUN_TTL_MS, failedTasks as failedAt, HINT_MAX, TASK_ATTENTION_CAP } from "./attention";

// "now" for every case: the fixtures' failures are all recent against it
const NOW = Date.parse("2026-10-12T12:00:00Z");
const failedTasks = (tasks: V2Task[]) => failedAt(tasks, NOW);
/** a naive ledger timestamp (UTC, no offset) `ms` before NOW */
const ago = (ms: number) => new Date(NOW - ms).toISOString().slice(0, 23);
const DAY = 24 * 60 * 60 * 1000;
const keys = (tasks: V2Task[]) => failedTasks(tasks).map((e) => e.key);

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
    expect(failedTasks([])).toEqual([]);
    const fine = (["queued", "running", "completed", "stopped", "paused", "pending"] as const).map((status, i) =>
      task(`t${i}`, { status }),
    );
    expect(failedTasks(fine)).toEqual([]);
  });

  it("queues failed runs and online tasks as act, keyed by kind", () => {
    const entries = failedTasks([task("r"), task("o", { kind: "online" }), task("ok", { status: "completed" })]);
    expect(entries.map((e) => [e.key, e.signal])).toEqual([
      ["run:r", "act"],
      ["online:o", "act"],
    ]);
  });

  it("orders newest first across naive and zoned timestamps", () => {
    const entries = failedTasks([
      task("old", { createdAt: "2026-10-10T08:00:00" }),
      // 2026-10-12 07:00 UTC — earlier than the naive 08:00 UTC below
      task("zoned", { createdAt: "2026-10-12T15:00:00+08:00" }),
      task("new", { createdAt: "2026-10-12T08:00:00" }),
      task("none", { createdAt: null }),
    ]);
    expect(entries.map((e) => e.task.id)).toEqual(["new", "zoned", "old", "none"]);
  });

  it(`returns every failure for the panel to cap at ${TASK_ATTENTION_CAP}`, () => {
    const many = Array.from({ length: 8 }, (_, i) => task(`t${i}`, { createdAt: `2026-10-12T0${i}:00:00` }));
    const entries = failedTasks(many);
    expect(entries.map((e) => e.task.id)).toEqual(["t7", "t6", "t5", "t4", "t3", "t2", "t1", "t0"]);
    expect(entries.slice(0, TASK_ATTENTION_CAP).map((e) => e.task.id)).toEqual(["t7", "t6", "t5", "t4", "t3"]);
  });

  it("shows the first line of the error, clipped, falling back to the online failure reason", () => {
    const run = task("r", { run: { error: "  Boom: judge failed\nTraceback …" } as V2Task["run"] });
    const online = task("o", { kind: "online", online: { failure_reason: "role missing" } as V2Task["online"] });
    const bare = task("b");
    const long = task("l", { run: { error: "x".repeat(300) } as V2Task["run"] });
    const exact = task("e", { run: { error: "y".repeat(HINT_MAX) } as V2Task["run"] });
    const detail = Object.fromEntries(failedTasks([run, online, bare, long, exact]).map((e) => [e.task.id, e.detail]));
    expect(detail.r).toBe("Boom: judge failed");
    expect(detail.o).toBe("role missing");
    expect(detail.b).toBe("");
    expect(detail.l).toHaveLength(HINT_MAX);
    expect(detail.l.endsWith("…")).toBe(true);
    expect(detail.e).toBe("y".repeat(HINT_MAX));
  });

  describe("failed run expiry", () => {
    const ds = (id: string) => ({ dataset_id: id }) as V2Task["run"];

    it("lists a failed run for 7 days after it was created, not a moment longer", () => {
      expect(keys([task("edge", { createdAt: ago(FAILED_RUN_TTL_MS) })])).toEqual(["run:edge"]);
      expect(keys([task("old", { createdAt: ago(FAILED_RUN_TTL_MS + 1) })])).toEqual([]);
      expect(keys([task("today", { createdAt: ago(60_000) })])).toEqual(["run:today"]);
      // a time that cannot be read is no proof of age: kept
      expect(keys([task("none", { createdAt: null })])).toEqual(["run:none"]);
    });

    it("drops a failure once a later run of the same agent on the same dataset completes", () => {
      const failed = task("f", { createdAt: ago(2 * DAY), run: ds("d1") });
      expect(keys([failed, task("ok", { status: "completed", createdAt: ago(DAY), run: ds("d1") })])).toEqual([]);
      // a completed run from before the failure fixed nothing
      expect(keys([failed, task("ok", { status: "completed", createdAt: ago(3 * DAY), run: ds("d1") })])).toEqual(["run:f"]);
    });

    it("keeps a failure that a later run only failed again", () => {
      const tasks = [
        task("f1", { createdAt: ago(2 * DAY), run: ds("d1") }),
        task("f2", { createdAt: ago(DAY), run: ds("d1") }),
        task("stopped", { status: "stopped", createdAt: ago(DAY), run: ds("d1") }),
      ];
      expect(keys(tasks)).toEqual(["run:f2", "run:f1"]);
    });

    it("is not superseded by a completed run on another benchmark", () => {
      const failed = task("f", { createdAt: ago(2 * DAY), run: ds("d1") });
      const later = { status: "completed" as const, createdAt: ago(DAY) };
      expect(keys([failed, task("other-ds", { ...later, run: ds("d2") })])).toEqual(["run:f"]);
      expect(keys([failed, task("cloud", { ...later, source: "cloud", run: ds("d1") })])).toEqual(["run:f"]);
      expect(keys([failed, task("other-agent", { ...later, agentId: "a2", run: ds("d1") })])).toEqual(["run:f"]);
      expect(keys([failed, task("insights", { ...later, mode: "insights", run: ds("d1") })])).toEqual(["run:f"]);
    });

    it("matches a run with no dataset by its scope kind", () => {
      const failed = task("w", { source: "window", createdAt: ago(2 * DAY) });
      const later = { status: "completed" as const, createdAt: ago(DAY) };
      expect(keys([failed, task("w2", { ...later, source: "window" })])).toEqual([]);
      expect(keys([failed, task("s", { ...later, source: "sessions" })])).toEqual(["run:w"]);
      // a log source counts as its own agent, by service
      const logs = (id: string, service: string, over: Partial<V2Task> = {}) =>
        task(id, { agentId: null, source: "logs", logSource: { service_name: service, log_group_names: [] }, ...over });
      const fl = logs("fl", "svc", { createdAt: ago(2 * DAY) });
      expect(keys([fl, logs("ok", "svc", later)])).toEqual([]);
      expect(keys([fl, logs("ok", "other", later)])).toEqual(["run:fl"]);
    });

    it("leaves failed online configs alone", () => {
      const online = task("o", { kind: "online", source: "live", createdAt: ago(30 * DAY) });
      const fixed = task("ok", { status: "completed", source: "live", createdAt: ago(DAY) });
      expect(keys([online, fixed])).toEqual(["online:o"]);
    });
  });
});

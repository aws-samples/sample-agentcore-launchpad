import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { SkillLabJobInfo } from "../../../lib/api";
import { HINT_MAX, JOB_ATTENTION_CAP, jobParams, jobQueue, jobSignal } from "./attention";

const job = (id: string, over: Partial<SkillLabJobInfo> = {}) =>
  ({
    id,
    type: "eval",
    status: "running",
    queue_position: 0,
    progress: "",
    skill_source: null,
    taskset_id: "ts1",
    taskset_name: "set",
    split: "",
    params: {},
    error: null,
    created_at: "2026-10-12T08:00:00",
    started_at: null,
    finished_at: null,
    ...over,
  }) as SkillLabJobInfo;

describe("skill lab job queue", () => {
  // run in UTC+8 whatever the host zone: in UTC a naive string reads the same either way
  const hostTz = process.env.TZ;
  beforeAll(() => {
    process.env.TZ = "Asia/Shanghai";
  });
  afterAll(() => {
    if (hostTz === undefined) delete process.env.TZ;
    else process.env.TZ = hostTz;
  });

  it("is empty for no jobs and for finished ones", () => {
    expect(jobQueue([])).toEqual([]);
    expect(jobQueue([job("s", { status: "succeeded" }), job("c", { status: "cancelled" })])).toEqual([]);
  });

  it("makes queued / running wait (running live) and failed / interrupted act", () => {
    expect(jobSignal({ status: "queued" })).toBe("wait");
    expect(jobSignal({ status: "running" })).toBe("wait");
    expect(jobSignal({ status: "failed" })).toBe("act");
    expect(jobSignal({ status: "interrupted" })).toBe("act");
    const entries = jobQueue([job("q", { status: "queued" }), job("r")]);
    expect(entries.map((e) => [e.job.id, e.live])).toEqual([
      ["q", false],
      ["r", true],
    ]);
  });

  it("covers all three job types in one queue, with their own pages", () => {
    const entries = jobQueue([
      job("e", { type: "eval" }),
      job("t", { type: "train", status: "interrupted" }),
      job("g", { type: "taskgen", status: "queued" }),
    ]);
    expect(entries.map((e) => e.key).sort()).toEqual(["eval:e", "taskgen:g", "train:t"]);
    expect(jobParams({ type: "eval", id: "e" })).toEqual({ tab: "eval", view: "detail", id: "e" });
    expect(jobParams({ type: "train", id: "t" })).toEqual({ tab: "train", view: "detail", id: "t" });
    expect(jobParams({ type: "taskgen", id: "g" })).toEqual({ tab: "tasksets", view: "gen", id: "g" });
  });

  it("puts act before wait, newest first within each (naive times read as UTC)", () => {
    const entries = jobQueue([
      job("w-old", { created_at: "2026-10-10T08:00:00" }),
      job("a-old", { status: "failed", created_at: "2026-10-11T08:00:00" }),
      job("w-new", { status: "queued", created_at: "2026-10-12T08:00:00" }),
      // 07:00 UTC: earlier than the naive 08:00 UTC below
      job("a-zoned", { status: "interrupted", created_at: "2026-10-12T15:00:00+08:00" }),
      job("a-new", { status: "failed", created_at: "2026-10-12T08:00:00" }),
    ]);
    expect(entries.map((e) => e.job.id)).toEqual(["a-new", "a-zoned", "a-old", "w-new", "w-old"]);
  });

  it(`returns every job for the panel to cap at ${JOB_ATTENTION_CAP}`, () => {
    const many = Array.from({ length: 9 }, (_, i) => job(`j${i}`, { created_at: `2026-10-0${i + 1}T00:00:00` }));
    const entries = jobQueue([...many, job("x", { status: "failed", created_at: "2026-09-01T00:00:00" })]);
    expect(entries).toHaveLength(10);
    expect(entries.slice(0, JOB_ATTENTION_CAP).map((e) => e.job.id)).toEqual(["x", "j8", "j7", "j6", "j5", "j4"]);
  });

  it("shows the first line of the error, clipped", () => {
    const entries = jobQueue([
      job("a", { status: "failed", error: "worker died\nTraceback …" }),
      job("b", { status: "failed", error: "z".repeat(400) }),
      job("c"),
    ]);
    const detail = Object.fromEntries(entries.map((e) => [e.job.id, e.detail]));
    expect(detail.a).toBe("worker died");
    expect(detail.b).toHaveLength(HINT_MAX);
    expect(detail.c).toBe("");
  });
});

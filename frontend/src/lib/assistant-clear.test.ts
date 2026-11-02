import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type AssistantConversationFootprint, type JobInfo } from "./api";
import {
  clearCanaryList,
  clearCloudKinds,
  clearElapsed,
  clearStepPhase,
  clearInProgress,
  runConversationClear,
  shortConversationTitle,
} from "./assistant";

const noSleep = () => Promise.resolve();

function job(status: JobInfo["status"], payload: Record<string, unknown> = {}): JobInfo {
  return { id: "j1", type: "purge_assistant_conversation", status, error: null, events: [], payload };
}

afterEach(() => vi.restoreAllMocks());

describe("runConversationClear", () => {
  it("returns an inline (transcript-only) delete without polling", async () => {
    vi.spyOn(api, "assistantDeleteConversation").mockResolvedValue({
      deleted: true, conversation_id: "c1", operations_cleaned: [], datasets: [], agents: [],
    });
    const getJob = vi.spyOn(api, "getJob");
    const out = await runConversationClear("c1", { sleep: noSleep });
    expect(out.kind).toBe("done");
    expect(getJob).not.toHaveBeenCalled();
  });

  it("follows a clear job through its steps until it succeeds", async () => {
    vi.spyOn(api, "assistantDeleteConversation").mockResolvedValue({
      deleted: false, conversation_id: "c1", job_id: "j1", status: "queued",
      operations_cleaned: [], datasets: [], agents: [],
    });
    vi.spyOn(api, "getJob")
      .mockResolvedValueOnce(job("running", {
        step: { key: "canary:abc", state: "waiting", detail: "endpoint:ctlabc (DELETING)" },
      }))
      .mockResolvedValueOnce(job("succeeded", {
        result: { agents: [{ id: "a1", name: "kid", aws_resource_deleted: true }],
                  operations_cleaned: [], datasets: [] },
      }));
    const progress: string[] = [];
    const out = await runConversationClear("c1", { sleep: noSleep, onProgress: (t) => progress.push(t) });
    expect(progress).toEqual(["canary:abc · waiting · endpoint:ctlabc (DELETING)"]);
    expect(out.kind === "done" && out.result.agents.map((a) => a.id)).toEqual(["a1"]);
  });

  it("reports a failed job's blocker instead of success", async () => {
    vi.spyOn(api, "assistantDeleteConversation").mockResolvedValue({
      deleted: false, conversation_id: "c1", job_id: "j1",
      operations_cleaned: [], datasets: [], agents: [],
    });
    vi.spyOn(api, "getJob").mockResolvedValue(job("failed", {
      blocker: { code: "assistant.purge_still_deleting", message: "still DELETING", detail: {}, status_code: 409 },
    }));
    const out = await runConversationClear("c1", { sleep: noSleep });
    expect(out).toEqual({ kind: "failed", reason: "still DELETING" });
  });

  it("stops following once detached, leaving the job to the server", async () => {
    vi.spyOn(api, "assistantDeleteConversation").mockResolvedValue({
      deleted: false, conversation_id: "c1", job_id: "j1",
      operations_cleaned: [], datasets: [], agents: [],
    });
    const getJob = vi.spyOn(api, "getJob").mockResolvedValue(job("running"));
    let polls = 0;
    const out = await runConversationClear("c1", {
      sleep: noSleep,
      detached: () => polls++ > 1,
    });
    expect(out.kind).toBe("detached");
    expect(getJob).toHaveBeenCalledTimes(2);
  });
});

describe("clear footprint helpers", () => {
  const fp = {
    canaries: [{
      id: "abc", name: "CANARY-kid", kind: "harness", status: "running", agent_id: "a1",
      running_action: null, pending: true,
      resources: { ab_test: true, gateway: "gw-1", endpoints: ["ctlabc", "trtabc"], online_evaluations: 2 },
    }],
    purge: { job_id: "j1", status: "running", error: null, blocker: null, step: null, updated_at: null },
  } as unknown as AssistantConversationFootprint;

  it("names every resource a canary owns", () => {
    expect(clearCanaryList(fp)).toBe(
      "CANARY-kid (running · A/B test · 2 online eval · gateway gw-1 · endpoints ctlabc, trtabc)",
    );
  });

  it("treats a queued or running job as in progress", () => {
    expect(clearInProgress(fp)).toBe(true);
    expect(clearInProgress({ ...fp, purge: { ...fp.purge!, status: "failed" } })).toBe(false);
    expect(clearInProgress({ ...fp, purge: null })).toBe(false);
  });
});

describe("clearCloudKinds", () => {
  const op = (cloud_kinds: Record<string, number> | undefined, cloud_resources = 0) => ({
    id: "o", status: "succeeded", plan_revision: 1, dataset_id: null, cloud_resources, cloud_kinds,
  });

  it("names only the kinds the plan created, in dialog order", () => {
    // prod QA 2026-11-02: one custom evaluator, no Lambda, a grant on the shared role
    expect(clearCloudKinds([op({ role_grant: 1, evaluator: 1 }, 2)])).toEqual([
      { kind: "evaluator", count: 1 },
      { kind: "role_grant", count: 1 },
    ]);
  });

  it("sums kinds across operations and keeps unknown kinds visible", () => {
    expect(
      clearCloudKinds([op({ evaluator: 2, lambda_function: 1 }, 3), op({ evaluator: 1, gizmo: 2 }, 3)]),
    ).toEqual([
      { kind: "evaluator", count: 3 },
      { kind: "lambda_function", count: 1 },
      { kind: "other", count: 2 },
    ]);
  });

  it("falls back to the bare total from a server without per-kind counts", () => {
    expect(clearCloudKinds([op(undefined, 4)])).toEqual([{ kind: "unknown", count: 4 }]);
    expect(clearCloudKinds([op({}, 0)])).toEqual([]);
  });
});

describe("shortConversationTitle", () => {
  it("keeps a short title and falls back on a blank one", () => {
    expect(shortConversationTitle("HR 助手", "c1")).toBe("HR 助手");
    expect(shortConversationTitle("  ", "c1")).toBe("c1");
    expect(shortConversationTitle(null, "c1")).toBe("c1");
  });

  it("cuts a paragraph-long title to one line with an ellipsis", () => {
    const long = "公司差旅报销政策问答助手，不需要外部系统。面向全体员工，在内部聊天里回答差旅标准、报销流程";
    const short = shortConversationTitle(long, "c1");
    expect(Array.from(short)).toHaveLength(40);
    expect(short.endsWith("…")).toBe(true);
    expect(long.startsWith(short.slice(0, -1))).toBe(true);
  });

  it("counts code points, so an emoji is never split", () => {
    const short = shortConversationTitle("😀".repeat(50), "c1", 10);
    expect(Array.from(short)).toEqual([...Array(9).fill("😀"), "…"]);
  });
});

describe("clearStepPhase", () => {
  it("names the phase of a per-resource step key", () => {
    expect(clearStepPhase({ key: "agent:a1", state: "deleting", detail: "deleting harness h1" })).toEqual({
      phase: "agents", done: false,
    });
    expect(clearStepPhase({ key: "operation:o1", state: "done", detail: "" })).toEqual({ phase: "operations", done: true });
    expect(clearStepPhase({ key: "canary:c1", state: "cleaning", detail: "" })?.phase).toBe("canaries");
    expect(clearStepPhase({ key: "ledger", state: "done", detail: "" })?.phase).toBe("ledger");
    expect(clearStepPhase({ key: "canary_artifacts", state: "done", detail: "" })?.phase).toBe("canary_artifacts");
  });

  it("leaves an unknown or missing step to the raw text", () => {
    expect(clearStepPhase({ key: "gizmo:1", state: "x", detail: "" })).toBeNull();
    expect(clearStepPhase(null)).toBeNull();
  });
});

describe("clearElapsed", () => {
  it("splits the elapsed time into minutes and seconds", () => {
    expect(clearElapsed(1_000, 1_000)).toEqual({ minutes: 0, seconds: 0 });
    expect(clearElapsed(0, 245_900)).toEqual({ minutes: 4, seconds: 5 });
    expect(clearElapsed(10_000, 0)).toEqual({ minutes: 0, seconds: 0 });
  });
});

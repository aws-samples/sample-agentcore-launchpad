import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type AssistantConversationFootprint, type JobInfo } from "./api";
import { clearCanaryList, clearInProgress, runConversationClear } from "./assistant";

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

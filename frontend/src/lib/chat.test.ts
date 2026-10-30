import { describe, expect, it, vi } from "vitest";

import type { AgentInfo, ChatMemorySummary } from "./api";
import { isHarnessAgent, markTruncated, pollMemorySummary, staleAgentState } from "./chat";

/** Minimal fixture: isHarnessAgent only reads `method` and `spec.discovery`. */
function agent(method: AgentInfo["method"], discovery?: Record<string, unknown>): AgentInfo {
  return {
    id: "a1",
    name: "a1",
    method,
    status: "active",
    arn: "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/a1",
    resource_id: "a1",
    version: "1",
    created_by: "u_member",
    created_by_name: "member",
    error: null,
    spec: discovery ? { discovery } : {},
    canary_capability: { eligible: false, reason: null, reason_code: null },
    invoke_capability: { eligible: true, reason: null, reason_code: null },
    created_at: null,
    updated_at: null,
  };
}

describe("isHarnessAgent (R16: self-built Harness is Runtime-backed, N1 discovery residue is not)", () => {
  it("is false for undefined", () => {
    expect(isHarnessAgent(undefined)).toBe(false);
  });

  it("is false for the self-built Harness method — it stops a session like every other Runtime method", () => {
    expect(isHarnessAgent(agent("harness"))).toBe(false);
  });

  it("is false for every other self-built method", () => {
    expect(isHarnessAgent(agent("zip_runtime"))).toBe(false);
    expect(isHarnessAgent(agent("studio"))).toBe(false);
    expect(isHarnessAgent(agent("byoc"))).toBe(false);
  });

  it("is true only for a discovered row whose scanned resource is a managed Harness", () => {
    expect(isHarnessAgent(agent("discovered_runtime", { resource_type: "harness" }))).toBe(true);
  });

  it("is false for a discovered row that is an ordinary Runtime", () => {
    expect(isHarnessAgent(agent("discovered_runtime", { resource_type: "runtime" }))).toBe(false);
    expect(isHarnessAgent(agent("discovered_runtime"))).toBe(false);
  });
});

describe("pollMemorySummary (N10-bug6: the rail re-reads until the saved turn is visible)", () => {
  const summary = (event_count: number): ChatMemorySummary => ({
    event_count,
    records: [],
    actor_id: event_count ? "agent__member" : undefined,
  });
  /** `fetch` yields the scripted counts in order; `sleep` records its delays and never waits. */
  function harness(counts: (number | Error)[]) {
    let i = 0;
    const fetch = vi.fn(async () => {
      const next = counts[Math.min(i++, counts.length - 1)];
      if (next instanceof Error) throw next;
      return summary(next);
    });
    const delays: number[] = [];
    const sleep = vi.fn(async (ms: number) => {
      delays.push(ms);
    });
    const seen: number[] = [];
    const onUpdate = (s: ChatMemorySummary) => seen.push(s.event_count);
    return { fetch, sleep, delays, seen, onUpdate };
  }

  it("stops at the first read that already shows the new event (no sleep)", async () => {
    const h = harness([1]);
    const out = await pollMemorySummary(h.fetch, 0, { sleep: h.sleep, onUpdate: h.onUpdate });
    expect(out?.event_count).toBe(1);
    expect(h.fetch).toHaveBeenCalledTimes(1);
    expect(h.delays).toEqual([]);
    expect(h.seen).toEqual([1]);
  });

  it("retries while the count has not grown, reporting each read to the rail", async () => {
    const h = harness([0, 0, 1]);
    const out = await pollMemorySummary(h.fetch, 0, { delayMs: 10, sleep: h.sleep, onUpdate: h.onUpdate });
    expect(out?.event_count).toBe(1);
    expect(h.fetch).toHaveBeenCalledTimes(3);
    expect(h.delays).toEqual([10, 10]);
    expect(h.seen).toEqual([0, 0, 1]);
  });

  it("compares against the pre-turn count, not zero", async () => {
    const h = harness([3, 3, 4]);
    const out = await pollMemorySummary(h.fetch, 3, { sleep: h.sleep });
    expect(out?.event_count).toBe(4);
    expect(h.fetch).toHaveBeenCalledTimes(3);
  });

  it("gives up after `attempts` and still returns the last read (agent-defined memory may never write)", async () => {
    const h = harness([0]);
    const out = await pollMemorySummary(h.fetch, 0, { attempts: 3, sleep: h.sleep, onUpdate: h.onUpdate });
    expect(out?.event_count).toBe(0);
    expect(h.fetch).toHaveBeenCalledTimes(3);
    expect(h.delays).toHaveLength(2);
    expect(h.seen).toEqual([0, 0, 0]);
  });

  it("treats a failed read as not-yet and retries; null when every read failed", async () => {
    const flaky = harness([new Error("503"), 1]);
    expect((await pollMemorySummary(flaky.fetch, 0, { sleep: flaky.sleep }))?.event_count).toBe(1);
    expect(flaky.fetch).toHaveBeenCalledTimes(2);
    const dead = harness([new Error("503")]);
    await expect(pollMemorySummary(dead.fetch, 0, { attempts: 2, sleep: dead.sleep })).resolves.toBeNull();
    expect(dead.fetch).toHaveBeenCalledTimes(2);
  });
});

type Item = { kind: string; streaming?: boolean; truncated?: boolean };

/** N-SCN-bug4 follow-up (r8-a): the `truncated: true` SSE marker flags the open bubble. */
describe("markTruncated", () => {
  it("flags the last agent bubble", () => {
    const out = markTruncated<Item>([{ kind: "user" }, { kind: "agent", streaming: true }]);
    expect(out[1]).toEqual({ kind: "agent", streaming: true, truncated: true });
    expect(out[0]).toEqual({ kind: "user" });
  });

  it("skips consent cards that sit after the open bubble", () => {
    const out = markTruncated<Item>([{ kind: "agent" }, { kind: "auth" }]);
    expect(out[0].truncated).toBe(true);
    expect(out[1].truncated).toBeUndefined();
  });

  it("skips a policy deny card after the open bubble (D20: the card does not close the bubble)", () => {
    // `policy_denied` arrives mid-answer; the answer keeps streaming into the same bubble,
    // so a later `truncated` marker must still land on that bubble (same walk as appendDelta)
    const out = markTruncated<Item>([{ kind: "user" }, { kind: "agent", streaming: true }, { kind: "policy" }]);
    expect(out[1]).toEqual({ kind: "agent", streaming: true, truncated: true });
    expect(out[2].truncated).toBeUndefined();
    const mixed = markTruncated<Item>([{ kind: "agent" }, { kind: "policy" }, { kind: "auth" }]);
    expect(mixed[0].truncated).toBe(true);
  });

  it("returns the list unchanged when no agent bubble is open or it is already flagged", () => {
    const noBubble: Item[] = [{ kind: "user" }, { kind: "tool" }];
    expect(markTruncated(noBubble)).toBe(noBubble);
    const flagged: Item[] = [{ kind: "agent", truncated: true }];
    expect(markTruncated(flagged)).toBe(flagged);
    const empty: Item[] = [];
    expect(markTruncated(empty)).toBe(empty);
  });

  it("does not reach past a tool call into an earlier bubble", () => {
    // a tool call closed the previous bubble; a marker arriving before the next delta has
    // nothing to flag (the backend scopes the flag the same way)
    const list: Item[] = [{ kind: "agent" }, { kind: "tool" }];
    expect(markTruncated(list)).toBe(list);
  });
});

describe("staleAgentState (N16-obs76: the chat deep-link notice follows the agent's state)", () => {
  const agent = (over: Partial<AgentInfo>): AgentInfo =>
    ({ id: "a1", status: "active", invoke_capability: { eligible: true }, ...over }) as AgentInfo;

  it("says deploying for an agent the list has as deploying", () => {
    expect(staleAgentState("a1", [agent({ status: "deploying" })])).toBe("deploying");
  });

  it("says inactive for a listed agent that is failed, draft or not invocable", () => {
    expect(staleAgentState("a1", [agent({ status: "failed" })])).toBe("inactive");
    expect(staleAgentState("a1", [agent({ status: "draft" })])).toBe("inactive");
    expect(staleAgentState("a1", [agent({ invoke_capability: { eligible: false } } as Partial<AgentInfo>)])).toBe(
      "inactive",
    );
  });

  it("says missing only when the list has no such id (or it is deleted)", () => {
    expect(staleAgentState("a1", [])).toBe("missing");
    expect(staleAgentState("a1", [agent({ id: "other" })])).toBe("missing");
    expect(staleAgentState("a1", [agent({ status: "deleted" })])).toBe("missing");
  });
});

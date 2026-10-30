// Chat helpers shared by the classic `/chat` page and the native V2 page.
import type { AgentInfo, ChatMemorySummary, ChatStreamPayload } from "./api";

/** Parse a `text/event-stream` body into `{event, data}` frames. */
export async function* sseEvents(
  res: Response,
): AsyncGenerator<{ event: string; data: ChatStreamPayload }> {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) yield { event, data: JSON.parse(data) as ChatStreamPayload };
    }
  }
}

/**
 * An externally-owned, imported managed Harness has no session-stop operation.
 * The self-built Harness (`method === "harness"`) is Runtime-backed like every
 * other method and stops a session the same way.
 */
export function isHarnessAgent(agent: AgentInfo | undefined): boolean {
  if (!agent) return false;
  const discovery = agent.spec.discovery as { resource_type?: string } | undefined;
  return agent.method === "discovered_runtime" && discovery?.resource_type === "harness";
}

/**
 * What the console can truthfully say about an agent's AgentCore Memory.
 *
 * The console never writes memory itself — the deployed agent does, driven by
 * `spec.memory` for the methods Launchpad renders or configures. BYOC code and
 * discovered agents decide on their own, so their spec says nothing reliable.
 */
export type AgentMemoryState = "on" | "off" | "agent-defined";

export function agentMemoryState(agent: AgentInfo | undefined): AgentMemoryState {
  if (!agent || agent.method === "byoc" || agent.method === "discovered_runtime") {
    return "agent-defined";
  }
  const memory = agent.spec.memory as { short_term?: boolean; long_term?: boolean } | undefined;
  if (!memory) return "agent-defined";
  return memory.short_term || memory.long_term ? "on" : "off";
}

/** Agents the chat console can talk to: active and invoke-eligible. */
export function chatEligible(agents: AgentInfo[]): AgentInfo[] {
  return agents.filter((a) => a.status === "active" && a.invoke_capability.eligible);
}

/** Why a deep-linked agent is not in the picker (N16-obs76, NC-E04-05): the notice says
 *  "still deploying" / "not active" for an agent the list has, "no longer exists" only
 *  when it really has no such id. */
export type StaleAgentState = "deploying" | "inactive" | "missing";

export function staleAgentState(id: string, all: AgentInfo[]): StaleAgentState {
  const found = all.find((a) => a.id === id);
  if (!found || found.status === "deleted") return "missing";
  return found.status === "deploying" ? "deploying" : "inactive";
}

export interface MemoryPollOptions {
  /** Total fetches, first one immediate. */
  attempts?: number;
  /** Pause between fetches. */
  delayMs?: number;
  /** Injected for tests; defaults to a real timer. */
  sleep?: (ms: number) => Promise<void>;
  /** Called with every summary that arrives, so a rail can update progressively. */
  onUpdate?: (summary: ChatMemorySummary) => void;
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * Re-read a session's memory summary until the turn that just finished is
 * visible (N10-bug6).
 *
 * The runtime writes the short-term event (`memory.create_event`) after it has
 * streamed `done`, so one fetch right after the stream usually still sees the
 * pre-turn count. Fetch until `event_count` exceeds `previous` or the attempts
 * run out; a failed fetch counts as "not yet" and is retried. Resolves with the
 * last summary read (or `null` when every fetch failed) — never throws, the rail
 * is best-effort.
 */
export async function pollMemorySummary(
  fetchSummary: () => Promise<ChatMemorySummary>,
  previous: number,
  { attempts = 4, delayMs = 1500, sleep = defaultSleep, onUpdate }: MemoryPollOptions = {},
): Promise<ChatMemorySummary | null> {
  let last: ChatMemorySummary | null = null;
  for (let i = 0; i < attempts; i++) {
    if (i > 0) await sleep(delayMs);
    try {
      last = await fetchSummary();
      onUpdate?.(last);
      if (last.event_count > previous) return last;
    } catch {
      /* not written yet / transient — retry */
    }
  }
  return last;
}

/**
 * N-SCN-bug4: flag the open agent bubble (the one the `truncated: true` marker closes —
 * skipping consent cards that may sit after it) as truncated. Pure; shared by the classic
 * and V2 chat pages. No open agent bubble → the list is returned unchanged.
 */
export function markTruncated<T extends { kind: string; truncated?: boolean }>(messages: T[]): T[] {
  let i = messages.length - 1;
  // consent asks and policy deny cards sit after the open bubble without closing it
  // (mirror of lib/chat-turn.ts::appendDelta, the delta reducer of both Chat pages)
  while (i >= 0 && (messages[i].kind === "auth" || messages[i].kind === "policy")) i -= 1;
  if (i < 0 || messages[i].kind !== "agent" || messages[i].truncated) return messages;
  const next = [...messages];
  next[i] = { ...next[i], truncated: true };
  return next;
}

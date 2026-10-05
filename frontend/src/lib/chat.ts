// Chat helpers shared by the classic `/chat` page and the native V2 page.
import type { AgentInfo, ChatStreamPayload } from "./api";

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

/** A managed Harness (deployed or imported) has no session-stop operation. */
export function isHarnessAgent(agent: AgentInfo | undefined): boolean {
  if (!agent) return false;
  if (agent.method === "harness") return true;
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

/**
 * "Invoke as me" on a JWT-inbound agent. `null` = auto: the field is omitted
 * and the backend presents the member's pool JWT when there is one, else the
 * workspace M2M token. Only an explicit toggle sends `as_user` — an explicit
 * `true` without a pool sign-in is refused (409 `chat.as_user_unavailable`)
 * rather than silently becoming the machine identity.
 */
export type AsUserChoice = boolean | null;

/** The `as_user` request field for a choice (undefined ⇒ omitted, auto). */
export function asUserField(jwtAgent: boolean, choice: AsUserChoice): boolean | undefined {
  return jwtAgent && choice !== null ? choice : undefined;
}

/** What the checkbox shows: the explicit choice, else what auto resolves to
 *  (the member's own JWT exactly when signed in through the pool). */
export function asUserChecked(choice: AsUserChoice, signedIn: boolean): boolean {
  return choice ?? signedIn;
}

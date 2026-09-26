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

/** Agents the chat console can talk to: active and invoke-eligible. */
export function chatEligible(agents: AgentInfo[]): AgentInfo[] {
  return agents.filter((a) => a.status === "active" && a.invoke_capability.eligible);
}

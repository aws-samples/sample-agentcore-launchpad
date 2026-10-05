/** What AgentCore Identity appends to the return URL. `state` is the
 *  customState the generated agent sent: `{agent_id, tool, session_id}`. */
export interface ReturnParams {
  sessionUri: string | null;
  agentId: string | null;
  tool: string | null;
  chatSession: string | null;
}

export function parseReturnParams(search: string): ReturnParams {
  const params = new URLSearchParams(search);
  let state: Record<string, unknown> = {};
  try {
    const parsed: unknown = JSON.parse(params.get("state") ?? "");
    if (parsed && typeof parsed === "object") state = parsed as Record<string, unknown>;
  } catch {
    // an IdP that mangles state still completes: only the way back is lost
  }
  const str = (v: unknown) => (typeof v === "string" && v ? v : null);
  return {
    sessionUri: str(params.get("session_id")),
    agentId: str(state.agent_id),
    tool: str(state.tool),
    chatSession: str(state.session_id),
  };
}

/** The Chat conversation that asked for consent, for when the tab can't close. */
export function chatPathFor(ret: ReturnParams): string {
  if (!ret.agentId) return "/v2/chat";
  const q = new URLSearchParams({ agent: ret.agentId });
  if (ret.chatSession) q.set("session", ret.chatSession);
  return `/v2/chat?${q.toString()}`;
}

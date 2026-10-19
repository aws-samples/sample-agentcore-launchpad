/** What AgentCore Identity appends to the return URL. `state` is the
 *  customState the asker sent: `{agent_id, tool, session_id}` from a Chat turn,
 *  or `{kind: "context_layer"}` from the Context Layer page. */
export interface ReturnParams {
  sessionUri: string | null;
  agentId: string | null;
  tool: string | null;
  chatSession: string | null;
  /** the console surface that asked, when it was not a Chat turn */
  kind: string | null;
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
    kind: str(state.kind),
  };
}

/** Where a consent goes back to, for when the tab can't close: the Chat
 *  conversation that asked, or the Context Layer page for a module consent. */
export function chatPathFor(ret: ReturnParams): string {
  if (!ret.agentId && ret.kind === "context_layer") return "/v2/context-layer";
  if (!ret.agentId) return "/v2/chat";
  const q = new URLSearchParams({ agent: ret.agentId });
  if (ret.chatSession) q.set("session", ret.chatSession);
  return `/v2/chat?${q.toString()}`;
}

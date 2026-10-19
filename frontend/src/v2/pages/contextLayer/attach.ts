import { type ContextLayerChoice, NAMESPACE_ID_RE } from "../../../lib/agent-spec";
import type { AgentInfo } from "../../../lib/api";

/**
 * "Attach to agent" from a namespace view: a new agent opens the V2 wizard, an
 * existing one its re-publish editor, both with the namespace prefilled through
 * `context_ns` / `context_name`. The wizard reads them once, when its draft is
 * created; saving is the ordinary create / redeploy.
 */

export const CONTEXT_NS_PARAM = "context_ns";
export const CONTEXT_NAME_PARAM = "context_name";
/** `ContextLayerRef.namespace_name` max length (backend schema). */
export const NAMESPACE_NAME_MAX = 256;

/** The namespace a wizard URL prefills, or null. An id that is not a UUID is
 *  ignored rather than typed into the form. */
export function contextPrefill(params: URLSearchParams): ContextLayerChoice | null {
  const id = (params.get(CONTEXT_NS_PARAM) ?? "").trim();
  if (!NAMESPACE_ID_RE.test(id)) return null;
  const name = (params.get(CONTEXT_NAME_PARAM) ?? "").trim().slice(0, NAMESPACE_NAME_MAX);
  return { namespaceId: id, namespaceName: name };
}

function prefillQuery(ns: { id: string; name: string }): string {
  const q = new URLSearchParams({ [CONTEXT_NS_PARAM]: ns.id });
  const name = ns.name.trim().slice(0, NAMESPACE_NAME_MAX);
  if (name) q.set(CONTEXT_NAME_PARAM, name);
  return q.toString();
}

/** The wizard for a new agent of `method`, on its configure step. */
export const newAgentLink = (ns: { id: string; name: string }, method: "harness" | "zip_runtime"): string =>
  `/v2/agents?view=new&method=${method}&${prefillQuery(ns)}`;

/** An existing agent's re-publish editor. */
export const editAgentLink = (agentId: string, ns: { id: string; name: string }): string =>
  `/v2/agents?view=edit&id=${encodeURIComponent(agentId)}&${prefillQuery(ns)}`;

/** The namespace an agent's stored spec attaches, or null. */
export function attachedNamespace(agent: Pick<AgentInfo, "spec">): { id: string; name: string } | null {
  const ref = agent.spec?.context_layer as { namespace_id?: string; namespace_name?: string } | null | undefined;
  return ref?.namespace_id ? { id: ref.namespace_id, name: ref.namespace_name ?? "" } : null;
}

/**
 * The agents the member may attach a namespace to: a Harness or an HTTP Strands
 * runtime (no exported `code` bundle, which the backend refuses), not a system
 * preset, not mid-deploy, and only with the deploy permission — the same rule as
 * the agents list's Edit action.
 */
export function attachTargets(agents: AgentInfo[], canDeploy: boolean): AgentInfo[] {
  if (!canDeploy) return [];
  return agents.filter((agent) => {
    if (agent.system || agent.status === "deploying" || agent.status === "deleted") return false;
    if (agent.method === "harness") return true;
    if (agent.method !== "zip_runtime") return false;
    const spec = agent.spec ?? {};
    return spec.protocol !== "a2a" && !spec.code && !spec.code_bundle;
  });
}

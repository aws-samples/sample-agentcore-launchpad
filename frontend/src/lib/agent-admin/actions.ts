import { useState } from "react";

import { api, type AgentInfo } from "../api";

export type AgentActionKind = "delete" | "convert";

/** An imported runtime is "removed": only the platform record goes, the AWS runtime stays. */
export const isImported = (agent: Pick<AgentInfo, "method">) => agent.method === "discovered_runtime";

/** Deleting a platform agent asks for its resource name; removing an import does not. */
export function needsTypedName(kind: AgentActionKind, agent: Pick<AgentInfo, "method">): boolean {
  return kind === "delete" && !isImported(agent);
}

/** The typed confirmation matches the agent's resource name (surrounding spaces ignored). */
export function typedNameMatches(agent: Pick<AgentInfo, "name">, typed: string): boolean {
  return typed.trim() === agent.name;
}

/** Run a confirmed action: delete returns the agent it removed, convert the new code
 *  agent — filed under `projectId`, the business project the dialog picked (#356). */
export async function performAgentAction(
  kind: AgentActionKind,
  agent: AgentInfo,
  projectId?: string,
): Promise<{ kind: "delete"; agent: AgentInfo } | { kind: "convert"; agent: AgentInfo }> {
  if (kind === "delete") {
    await api.deleteAgent(agent.id);
    return { kind, agent };
  }
  const res = await api.convertAgent(agent.id, projectId || undefined);
  return { kind, agent: res.agent };
}

export interface AgentActionCallbacks {
  onDeleted: (agent: AgentInfo) => void;
  /** the new code agent the conversion created */
  onConverted: (agent: AgentInfo) => void;
  onError?: (err: unknown) => void;
}

/**
 * Delete / convert, behind a confirmation the caller draws. `ask*` opens it,
 * `confirm()` runs the call: success closes it and reports, a failure keeps it
 * open with `error` set (the raw error, so V3 can show the AWS detail).
 */
export function useAgentActions(callbacks: AgentActionCallbacks) {
  const [pending, setPending] = useState<{ kind: AgentActionKind; agent: AgentInfo } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const open = (kind: AgentActionKind, agent: AgentInfo) => {
    setError(null);
    setPending({ kind, agent });
  };

  /** `projectId`: a conversion's business project (the dialog's picker) */
  const confirm = async (projectId?: string) => {
    if (!pending) return;
    setBusy(true);
    setError(null);
    try {
      const done = await performAgentAction(pending.kind, pending.agent, projectId);
      if (done.kind === "delete") callbacks.onDeleted(done.agent);
      else callbacks.onConverted(done.agent);
      setPending(null);
    } catch (err) {
      setError(err);
      callbacks.onError?.(err);
    } finally {
      setBusy(false);
    }
  };

  return {
    pending,
    busy,
    error,
    askDelete: (agent: AgentInfo) => open("delete", agent),
    askConvert: (agent: AgentInfo) => open("convert", agent),
    cancel: () => {
      setPending(null);
      setError(null);
    },
    confirm,
  };
}

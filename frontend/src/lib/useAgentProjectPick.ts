import { useCallback, useEffect, useState } from "react";

import { useProject } from "../workspace/project-context";
import { useWorkspace } from "../workspace/workspace-context";
import { agentProjectLabel, pickedAgentProjectId } from "./agent-project";
import { type AgentCreatePermission, api, type EligibleAgentProject } from "./api";

/** The project picker's state, drawn by each console's own field. */
export interface AgentProjectPick {
  /** the eligible projects (server-filtered for `permission`); empty until loaded */
  rows: EligibleAgentProject[];
  loading: boolean;
  /** the raw failure of the list read (a console shows its AWS detail), else null */
  error: unknown;
  reload: () => void;
  /** the project the new agent is filed under; "" = none picked / none eligible */
  projectId: string;
  pick: (id: string) => void;
  /** `Department · Project` of the pick, for a review line */
  label: string | null;
}

/**
 * The business project a NEW agent is filed under (services/agent_project), for
 * every creation path: the server-filtered eligible list for the route's key
 * (`agents.deploy` / `agents.import` / `agents.convert`), starting on the shell's
 * project only when it is eligible — a `default` selection is never preselected.
 * The pick is sent as the body `project_id` (the typed client pins the matching
 * `X-Project`). `enabled: false` (a re-publish, a closed dialog) loads nothing.
 */
export function useAgentProjectPick(
  permission: AgentCreatePermission = "agents.deploy",
  enabled = true,
): AgentProjectPick {
  const { current: workspace } = useWorkspace();
  const { current: shellProject } = useProject();
  const [state, setState] = useState<{ rows: EligibleAgentProject[] | null; error: unknown; loading: boolean }>({
    rows: null,
    error: null,
    loading: enabled,
  });
  const [nonce, setNonce] = useState(0);
  const [picked, setPicked] = useState<string | null>(null);
  const workspaceId = workspace?.id ?? "";
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    setState((prev) => ({ ...prev, loading: true, error: null }));
    api
      .agentEligibleProjects(permission)
      .then(({ projects }) => live && setState({ rows: projects, error: null, loading: false }))
      .catch((error: unknown) => live && setState((prev) => ({ ...prev, error, loading: false })));
    return () => {
      live = false;
    };
  }, [enabled, permission, workspaceId, nonce]);
  const reload = useCallback(() => setNonce((n) => n + 1), []);
  const rows = state.rows ?? [];
  const projectId = enabled ? pickedAgentProjectId(rows, picked, shellProject?.id) : "";
  const row = rows.find((p) => p.id === projectId);
  return {
    rows,
    // nothing read yet (just enabled) reads as loading, never as an empty list
    loading: enabled && (state.loading || (state.rows === null && state.error == null)),
    error: enabled ? state.error : null,
    reload,
    projectId,
    pick: setPicked,
    label: row ? agentProjectLabel(row) : null,
  };
}

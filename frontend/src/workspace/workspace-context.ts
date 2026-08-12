import { createContext, useContext } from "react";

import type { Workspace } from "../lib/api";

export interface WorkspaceContextValue {
  /** every workspace for an admin; the granted ones for a member */
  workspaces: Workspace[];
  /** the environment every request in this console session targets */
  current: Workspace | null;
  /** the list is unfiltered, i.e. the caller is an administrator */
  allWorkspaces: boolean;
  /** switch environments: persists the choice and remounts the routed subtree */
  select: (id: string) => void;
  /** re-read the list after a register / delete / bootstrap */
  refresh: () => Promise<void>;
}

export const WorkspaceContext = createContext<WorkspaceContextValue>({
  workspaces: [],
  current: null,
  allWorkspaces: false,
  select: () => undefined,
  refresh: async () => undefined,
});

export function useWorkspace() {
  return useContext(WorkspaceContext);
}

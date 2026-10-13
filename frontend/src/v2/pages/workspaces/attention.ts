// The workspaces that are not usable yet and need an admin (ported from the V3 console).
// Pure: the list turns each entry into a row and owns the copy and the navigation.
import type { Workspace } from "../../../lib/api";
import { rankBySignal, type Signal } from "../../signals";

export type WorkspaceAttentionKind = "failed" | "registered";

export interface WorkspaceEntry {
  key: string;
  kind: WorkspaceAttentionKind;
  signal: Signal;
  workspace: Workspace;
}

/** A failed bootstrap needs a resume (act); a registered workspace was never bootstrapped
 *  (wait). The hub is never bootstrapped from here, so it is never queued. Act before
 *  wait; API order otherwise. */
export function workspaceAttention(workspaces: Workspace[]): WorkspaceEntry[] {
  const out: WorkspaceEntry[] = [];
  for (const w of workspaces) {
    if (w.is_default) continue;
    if (w.bootstrap_status === "failed") out.push({ key: `${w.id}:failed`, kind: "failed", signal: "act", workspace: w });
    else if (w.bootstrap_status === "registered") {
      out.push({ key: `${w.id}:registered`, kind: "registered", signal: "wait", workspace: w });
    }
  }
  return rankBySignal(out);
}

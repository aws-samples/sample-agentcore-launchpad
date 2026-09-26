import type { WorkspaceBootstrapStatus } from "../../../lib/api";
import type { TagTone } from "../../ui";

/** Only `ready` accepts mutating traffic; `failed` is retryable (resume). */
export const STATUS_TONE: Record<WorkspaceBootstrapStatus, TagTone> = {
  registered: "gray",
  bootstrapping: "blue",
  ready: "green",
  failed: "red",
};

export const WORKSPACE_STATUSES: WorkspaceBootstrapStatus[] = ["registered", "bootstrapping", "ready", "failed"];

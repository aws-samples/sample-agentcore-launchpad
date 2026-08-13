import type { ChipTone } from "../../components";
import type { WorkspaceBootstrapStatus } from "../../lib/api";

/** Only `ready` accepts mutating traffic; `failed` is retryable (resume). */
export const STATUS_TONE: Record<WorkspaceBootstrapStatus, ChipTone> = {
  registered: "blue",
  bootstrapping: "warn",
  ready: "good",
  failed: "crit",
};

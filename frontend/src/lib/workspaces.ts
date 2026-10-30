import { browserUuid } from "./browser-compat";
/**
 * Workspace registration + bootstrap helpers shared by the classic and V2
 * Workspaces pages (pure logic, no UI).
 */
import type { StageInfo } from "./api";

/**
 * Regions AgentCore serves. The list is a convenience, not the authority: the
 * bootstrap job's `validate-access` stage probes the target for real, so an
 * operator can type a region this build has not heard of.
 */
export const WORKSPACE_REGIONS = [
  "cn-northwest-1",
  "cn-north-1",
] as const;

/** `arn:aws…:iam::<account>:role/<name>`; group 1 is the account id. */
export const ROLE_ARN = /^arn:aws[a-z-]*:iam::(\d{12}):role\/.+$/;

/**
 * A suggestion for the ExternalId, not a secret the backend knows: the operator
 * has to deploy the spoke stack with the same value, so it is theirs to keep.
 * Uses cryptographic randomness on both HTTPS and the explicit HTTP lab entry.
 */
export function suggestExternalId(): string {
  return `launchpad-${browserUuid().replace(/-/g, "").slice(0, 20)}`;
}

/** The bootstrap job's stages, in order — mirrors backend
 *  `app/services/workspace_bootstrap.py::STAGE_ORDER` exactly (CN build: no
 *  Cognito stage; adds `model-credential` (C4), `memory` (N6, the
 *  self-hosted Memory/KB substrate — ruling R2), `skill-lab` and `policy` (D20)).
 *  `backend/tests/consistency/test_surface_consistency.py` pins the two lists equal
 *  (N16-obs50). */
export const BOOTSTRAP_STAGES = [
  "validate-access",
  "iam",
  "model-credential",
  "storage",
  "codebuild",
  "gateway",
  "registry",
  "memory",
  "skill-lab",
  "observability",
  "policy",
  "finalize",
] as const;

export const PENDING_BOOTSTRAP_STAGES: StageInfo[] = BOOTSTRAP_STAGES.map((name) => ({
  name,
  status: "pending",
  detail: "",
}));

/**
 * The job id of a running bootstrap, per workspace.
 *
 * A bootstrap outlives the page: the backend resumes an interrupted run, and
 * without this the console could no longer say which job to watch after a
 * reload (the latest-run endpoint covers other browsers).
 */
const JOB_STORE = "launchpad_ws_bootstrap_jobs";

export function readBootstrapJobIds(): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(JOB_STORE);
    return raw ? (JSON.parse(raw) as Record<string, string>) : {};
  } catch {
    return {};
  }
}

export function rememberBootstrapJobId(workspaceId: string, jobId: string): void {
  try {
    window.localStorage.setItem(
      JOB_STORE,
      JSON.stringify({ ...readBootstrapJobIds(), [workspaceId]: jobId }),
    );
  } catch {
    /* storage unavailable — progress is still visible in this session */
  }
}

/** `table: n · table: n` for a ledger-row count map (purge preview, detach 409). */
export function formatRowCounts(rows: Record<string, number>): string {
  return Object.entries(rows)
    .map(([table, count]) => `${table}: ${count}`)
    .join(" · ");
}

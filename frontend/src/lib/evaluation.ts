export interface EvaluationDatasetInfo {
  id: string;
  name: string;
  kind?: string;
  locale: string;
  item_count: number;
  has_ground_truth?: boolean;
}

export interface CloudDatasetInfo {
  datasetId: string;
  name: string;
  status: string;
  schemaType: string;
  exampleCount: number | null;
}

export interface EvaluationScore {
  evaluatorId: string;
  score: number;
}

export interface InsightCluster {
  clusterId?: number;
  name?: string;
  category?: string;
  description?: string;
  percentage?: number;
  affectedSessionCount?: number;
  affectedSessions?: {
    sessionId?: string;
    userMessages?: string[];
    approachTaken?: string;
    finalOutcome?: string;
  }[];
  subCategories?: {
    name?: string;
    rootCauses?: { name?: string; recommendation?: string }[];
  }[];
}

/** The three insight trees `parse_insights` yields — shared by batch insights
 *  runs (`EvaluationRunInfo.insights`) and online-config reports. */
export interface InsightTrees {
  failures?: InsightCluster[];
  userIntents?: InsightCluster[];
  executionSummaries?: InsightCluster[];
}

export const hasInsightTrees = (insights: InsightTrees | null | undefined): boolean =>
  (insights?.failures?.length ?? 0) > 0 ||
  (insights?.userIntents?.length ?? 0) > 0 ||
  (insights?.executionSummaries?.length ?? 0) > 0;

/** Ledger run states. `stopped` is terminal like `completed`/`failed` — an
 *  operator stop (queue cancel, replay abort, or AWS StopBatchEvaluation with
 *  the sessions judged so far kept). */
export type EvaluationRunStatus =
  | "queued"
  | "invoking"
  | "waiting"
  | "evaluating"
  | "completed"
  | "failed"
  | "stopped";

export const RUN_TERMINAL_STATUSES: ReadonlySet<EvaluationRunStatus> = new Set([
  "completed",
  "failed",
  "stopped",
]);

/** Still queued / running — the only runs STOP applies to. */
export const isActiveRun = (run: { status: EvaluationRunStatus }): boolean =>
  !RUN_TERMINAL_STATUSES.has(run.status);

export interface EvaluationRunInfo {
  id: string;
  agent_id: string;
  agent_name: string;
  dataset_id?: string | null;
  dataset_name: string | null;
  mode: string;
  evaluators: string[];
  status: EvaluationRunStatus;
  /** an operator stop is pending (in-memory flag; the row turns `stopped` once
   *  the worker/poller observes it). Absent on older backends. */
  stop_requested?: boolean;
  queue_position: number | null;
  scores: EvaluationScore[];
  insights: InsightTrees;
  session_ids: string[];
  /** The AWS batch evaluation behind this run; absent for window-scoped runs that
   *  never started one. Required to pin RECOMMEND to this run's sessions. */
  batch_eval_id?: string | null;
  error: string | null;
  created_at?: string | null;
}

export interface ExperimentReadiness {
  agent_id: string;
  lookback_hours: number;
  state: "missing" | "sparse" | "ready" | "unavailable";
  trace_count: number;
  session_count: number;
  latest_trace_at: string | null;
  observed_tools: string[];
  expected_tools: string[];
  missing_tools: string[];
  latest_run: {
    id: string;
    status: string;
    session_count: number;
    created_at: string | null;
  } | null;
  message: string | null;
}

export const CLOUD_VALUE_PREFIX = "cloud:";
export const SIMULATED_SCHEMA = "AGENTCORE_EVALUATION_SIMULATED_V1";
export const DEFAULT_EVALUATORS = ["Builtin.Correctness", "Builtin.Helpfulness"];

export const ACTOR_MODELS = [
  "global.anthropic.claude-haiku-4-5-20251001-v1:0",
  "global.anthropic.claude-sonnet-5",
  "global.anthropic.claude-sonnet-4-6",
  "global.amazon.nova-2-lite-v1:0",
];

export const ACTIVE_RUN_STATUSES = new Set([
  "queued",
  "invoking",
  "waiting",
  "evaluating",
]);

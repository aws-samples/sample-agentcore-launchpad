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

export interface EvaluationRunInfo {
  id: string;
  agent_id: string;
  agent_name: string;
  dataset_id?: string | null;
  dataset_name: string | null;
  mode: string;
  evaluators: string[];
  status: string;
  queue_position: number | null;
  scores: EvaluationScore[];
  insights: {
    failures?: InsightCluster[];
    userIntents?: InsightCluster[];
    executionSummaries?: InsightCluster[];
  };
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

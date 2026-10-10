import { evaluatorPolarity } from "./evaluators";

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
  /** Judgements this average covers (AWS `totalEvaluated`); absent on rows
   *  recorded before it was stored. */
  count?: number;
}

/** A run's cross-evaluator mean on the task detail's 0..1 "higher is better"
 *  scale: penalty evaluators (Refusal) are flipped, and each evaluator weighs by
 *  the judgements it averages (1 when `count` is absent), so it matches the
 *  normalized mean over every result row. */
export function runMeanScore(scores: EvaluationScore[]): number | null {
  let total = 0;
  let weight = 0;
  for (const s of scores) {
    if (typeof s.score !== "number") continue;
    const w = s.count && s.count > 0 ? s.count : 1;
    total += (evaluatorPolarity(s.evaluatorId) < 0 ? 1 - s.score : s.score) * w;
    weight += w;
  }
  return weight > 0 ? total / weight : null;
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

/** An agent's telemetry in CloudWatch Logs — `StartBatchEvaluation`'s `cloudWatchLogs` source. */
export interface LogSource {
  service_name: string;
  log_group_names: string[];
}

export interface EvaluationRunInfo {
  id: string;
  agent_id: string;
  agent_name: string;
  /** set when the run evaluates CloudWatch telemetry with no platform agent
   *  (`agent_id` is "" and `agent_name` the service name) */
  log_source?: LogSource | null;
  dataset_id?: string | null;
  dataset_name: string | null;
  /** Published cloud-dataset version the run replayed ("2"); null = the draft
   *  (and every local / session / window run). Absent on older backends. */
  dataset_version?: string | null;
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
  /** dataset scenarios that ended on the agent's own budget (timeout after its
   *  replay, iteration / token limit) and are scored as they stand. Absent on older backends. */
  budget_stops?: EvaluationBudgetStop[];
  /** simulated persona scenarios skipped after their retries — the run went on
   *  with the rest. Absent on older backends. */
  scenario_failures?: EvaluationScenarioFailure[];
  /** Sessions a partially failed batch skipped, and why; null on a clean run.
   *  Absent on older backends / rows finished before it existed. */
  session_failures?: EvaluationSessionFailures | null;
  error: string | null;
  created_at?: string | null;
  /** operator-facing task name/description (console V2); null on unnamed runs */
  name?: string | null;
  description?: string | null;
  updated_at?: string | null;
}

export interface EvaluationScenarioFailure {
  scenario_id: string;
  /** retries spent before the scenario was skipped, as a string ("0", "1") */
  retries: string;
  code: string;
  error: string;
}

export interface EvaluationBudgetStop {
  scenario_id: string;
  session_id: string;
  code: string;
  stop_reason: string;
}

/** `telemetry_incomplete`: every error on the session is a missing/incomplete-span
 *  error (the session's traces never fully reached CloudWatch, so re-evaluating
 *  cannot recover it); `evaluator_error`: anything else. */
export type SessionFailureKind = "telemetry_incomplete" | "evaluator_error";

export interface EvaluationSessionFailures {
  total: number;
  failed: number;
  kind: SessionFailureKind | "mixed" | "unknown";
  /** failed sessions in dataset order; `index` = 1-based scenario position */
  sessions: { session_id: string; index: number | null; kind: SessionFailureKind; error_type: string; message: string }[];
}

export interface RunCoverage {
  total: number;
  scored: number;
  failed: number;
  kind: EvaluationSessionFailures["kind"];
  /** 1-based scenario positions that were not scored (sorted) */
  excluded: number[];
  firstError: string | null;
  /** finished before the summary existed: counts parsed from the AWS sentence */
  legacy: boolean;
}

const FAILED_SENTENCE = /(\d+)\s+of\s+(\d+)\s+sessions?\s+failed/i;

/** How much of a partially failed run was scored — null for a run with no error
 *  (or one that is not a completed evaluator run). */
export function runCoverage(run: Pick<EvaluationRunInfo, "status" | "error" | "session_failures" | "session_ids">): RunCoverage | null {
  if (run.status !== "completed" || !run.error?.trim()) return null;
  const f = run.session_failures;
  if (f) {
    const first = f.sessions[0];
    return {
      total: f.total,
      failed: f.failed,
      scored: Math.max(0, f.total - f.failed),
      kind: f.kind,
      excluded: f.sessions.map((s) => s.index).filter((i): i is number => i != null).sort((a, b) => a - b),
      firstError: first ? `${first.error_type}: ${first.message}` : null,
      legacy: false,
    };
  }
  const m = FAILED_SENTENCE.exec(run.error);
  if (!m) return null;
  const failed = Number(m[1]);
  const total = Number(m[2]) || run.session_ids.length;
  return { total, failed, scored: Math.max(0, total - failed), kind: "unknown", excluded: [], firstError: null, legacy: true };
}

/** Telemetry-only losses are informational (nothing to fix on the platform side):
 *  shown in the info (blue) tone instead of the warning tone. */
export function coverageInfo(coverage: RunCoverage | null): boolean {
  return coverage?.kind === "telemetry_incomplete";
}

/** True when two or more partial runs left different scenario positions unscored. */
export function coverageDiffers(coverages: (RunCoverage | null)[]): boolean {
  const keys = new Set(
    coverages.filter((c): c is RunCoverage => !!c && c.excluded.length > 0).map((c) => c.excluded.join(",")),
  );
  return keys.size > 1;
}

export type RecommendableReason = "evaluator_error" | "low_coverage" | "legacy" | "unknown" | "not_completed";

/** Whether a run may seed AI recommendations in the console: a clean completed
 *  batch, or one whose only losses are telemetry gaps and that still scored at
 *  least half of its sessions. */
export function runRecommendable(run: EvaluationRunInfo): { ok: boolean; reason?: RecommendableReason; coverage: RunCoverage | null } {
  if (run.status !== "completed" || !run.batch_eval_id) return { ok: false, reason: "not_completed", coverage: null };
  const coverage = runCoverage(run);
  if (!run.error?.trim()) return { ok: true, coverage: null };
  // a pre-summary row: "Read failure details" (re-check) can still qualify it
  if (coverage?.legacy) return { ok: false, reason: "legacy", coverage };
  if (!coverage || coverage.kind === "unknown") return { ok: false, reason: "unknown", coverage };
  if (coverage.kind !== "telemetry_incomplete") return { ok: false, reason: "evaluator_error", coverage };
  if (coverage.scored * 2 < coverage.total) return { ok: false, reason: "low_coverage", coverage };
  return { ok: true, coverage };
}

type EvaluationRunDisplayStatus = EvaluationRunStatus | "completed_with_errors";

const RUN_TONES = {
  queued: "muted",
  invoking: "warn",
  waiting: "warn",
  evaluating: "warn",
  completed: "good",
  completed_with_errors: "warn",
  failed: "crit",
  stopped: "muted",
} as const;

/** Display-only projection: partial completion remains a terminal `completed`
 *  ledger run, with available results and its original error retained. */
export function evaluationRunPresentation(run: Pick<EvaluationRunInfo, "status" | "error">) {
  const status: EvaluationRunDisplayStatus =
    run.status === "completed" && run.error?.trim() ? "completed_with_errors" : run.status;
  return { status, tone: RUN_TONES[status] };
}

/** One judge record of a batch run (`GET /api/eval/runs/{id}/results`) — the
 *  same columns SCORE NOW shows on the Observability session detail, plus the
 *  evaluation level: a span-level evaluator writes one record per tool call, so
 *  a session can carry several rows for one evaluator. */
export interface EvaluationRunResultRow {
  evaluator_id: string;
  level: string | null;
  score: number | null;
  label: string | null;
  explanation: string | null;
  error_type: string | null;
  error_message: string | null;
}

export interface EvaluationRunSessionResults {
  session_id: string;
  results: EvaluationRunResultRow[];
}

export type EvaluationRunResultsReason =
  | "insights_run"
  | "no_batch"
  | "run_active"
  | "stream_missing"
  | "unreadable";

export interface EvaluationRunResults {
  run_id: string;
  batch_eval_id: string | null;
  available: boolean;
  reason?: EvaluationRunResultsReason;
  detail?: string;
  /** run `session_ids` order first, then sessions only the stream knows */
  sessions: EvaluationRunSessionResults[];
  count: number;
  truncated: boolean;
}

/** Where a run recommendation's inputs came from: `harness` = live GetHarness (incl.
 *  the attached Gateways' tool schemas), `spec` = the Launchpad agent spec, `manual`
 *  = nothing readable — the operator must enter them. */
export type RunRecommendationSource = "harness" | "spec" | "manual";
export type RunRecommendationKind = "system_prompt" | "tool_descriptions";

export interface RunRecommendationEvaluator {
  id: string;
  name: string;
  level: string | null;
  group: "run" | "builtin" | "third_party" | "custom";
  /** the devguide's recommended targets: Builtin.GoalSuccessRate / Builtin.Helpfulness */
  recommended: boolean;
}

export interface RunRecommendationInputs {
  source: RunRecommendationSource;
  agent_method: string | null;
  system_prompt: string;
  tools: { name: string; description: string; origin: "inline_function" | "gateway" | "spec" }[];
  /** `harness_unreadable` (fell back to spec / manual), `gateway_unreadable`, `remote_mcp_runtime_only` */
  notes: { code: string; tool: string; detail: string }[];
  /** every evaluator the job can optimize toward: the run's own first, then the two
   *  recommended targets, then AWS built-ins, third-party and this account's custom ones */
  evaluators: RunRecommendationEvaluator[];
  /** left out and why — lower_is_better | ground_truth | categorical | unavailable */
  excluded_evaluators: { id: string; reason: string }[];
  default_evaluator: string;
  eligible: boolean;
  reason_code: "run_not_completed" | "run_no_batch" | null;
  /** tool jobs read the run's sessions' spans inline — false for a time-window run */
  tools_eligible: boolean;
}

export interface RunRecommendation {
  id: string;
  run_id: string;
  kind: RunRecommendationKind;
  recommendation_id: string;
  name: string;
  status: "PENDING" | "IN_PROGRESS" | "COMPLETED" | "FAILED" | "DELETING";
  input_source: RunRecommendationSource;
  system_prompt: string | null;
  evaluator: string | null;
  tools: Record<string, string>;
  skipped_tools: string[];
  result: {
    recommended_prompt?: string;
    explanation?: string;
    tools?: Record<string, { description: string; explanation: string }>;
    /** a 3rd-party provider row (e.g. gepa_lite): who produced it, with which model */
    provider?: string;
    provider_model_id?: string;
    provider_meta?: { evidence_sessions?: number; evidence_records?: number; latency_ms?: number };
    /** the provider job's last progress line while it runs */
    progress?: string;
    /** adversarial-test sessions left out of an AgentCore prompt recommendation's traces */
    excluded_sessions?: { session_id: string; scenario_id: string }[];
  };
  error: string | null;
  /** set once the recommended prompt was accepted into a new Harness version */
  accepted: {
    by: string;
    at: string;
    agent_id: string;
    previous_version: string | null;
    job_id: string;
    deployment_id: string;
    /** the published prompt was the operator's edit of the recommendation */
    edited?: boolean;
  } | null;
  /** an operator's saved revision of the recommended system prompt (null = none);
   *  the card, copy and accept use it in place of `result.recommended_prompt` */
  edit?: { prompt: string; by: string; at: string } | null;
  created_at: string | null;
  updated_at: string | null;
}

/** The cards a run shows by default: per kind the newest recommendation, plus any one
 *  that was accepted (it is what got published). Everything else — a superseded
 *  attempt, typically one AWS refused — is `earlier`, collapsed unless asked for.
 *  `recs` is newest-first, as the API lists it. */
export function splitRecommendations(recs: RunRecommendation[]): {
  current: RunRecommendation[];
  earlier: RunRecommendation[];
} {
  const seen = new Set<string>();
  const current: RunRecommendation[] = [];
  const earlier: RunRecommendation[] = [];
  for (const rec of recs) {
    if (!seen.has(rec.kind) || rec.accepted) current.push(rec);
    else earlier.push(rec);
    seen.add(rec.kind);
  }
  return { current, earlier };
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

/**
 * Models that can play a simulated persona's user — shared by the classic New Run
 * and the V2 task wizard; the first is the default in both. The SDK actor answers
 * through Strands structured output, which FORCES a tool call, so a model must
 * accept Converse `tool_choice` any/tool. Live-checked on the real
 * SimulatedScenarioExecutor 2026-10-10: GPT-6 Luna and Haiku 5.5 complete the
 * loop; Sonnet 5.5 rejects forced tool choice ("tool_choice: type tool and any
 * are not supported for this model").
 */
export const ACTOR_MODELS = [
  "global.openai.gpt-6-luna",
  "global.anthropic.claude-haiku-5-5",
  "global.anthropic.claude-sonnet-5",
  "global.anthropic.claude-haiku-4-5-20251001-v1:0",
  "global.amazon.nova-2-lite-v1:0",
];

export const ACTIVE_RUN_STATUSES = new Set([
  "queued",
  "invoking",
  "waiting",
  "evaluating",
]);

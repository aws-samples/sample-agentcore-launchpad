import type { RecommendProviderInfo, RunSnapshotRef } from "./api";
import { evaluatorPolarity } from "./evaluators";
import { ALLOWED_MODEL_IDS, MODEL_CATALOG } from "./models";

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
  /** EVAL-TRUST 3.2: sessions behind the interval (one observation per session), their mean,
   *  sample SD and the 95 % t-interval; absent on rows scored before it (read backfills). */
  sessions_n?: number;
  session_mean?: number | null;
  sd?: number | null;
  ci95?: [number, number] | null;
  /** N17-obs423: `wilson` for a binary evaluator, else `t`; the scale the interval is clamped
   *  to. Absent on rows summarised before the clamp (the read re-summarises them). */
  ci_method?: "t" | "wilson";
  scale?: [number, number];
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
  /** N17-obs449: one entry per worker shard — sessions the failure detector analysed, those
   *  whose detection did not complete (reply truncated / unparseable: an error row, never "no
   *  failures") and the findings the confidence threshold dropped */
  failureDetection?: FailureDetection[];
}

export interface FailureDetection {
  sessions: number;
  incomplete: number;
  lowConfidenceDropped: number;
  threshold?: string;
}

/** The run's failure-detection counts summed over its shards; null on an older run. */
export function failureDetectionTotals(insights: InsightTrees | null | undefined): FailureDetection | null {
  const parts = insights?.failureDetection;
  if (!parts?.length) return null;
  return parts.reduce<FailureDetection>(
    (acc, p) => ({
      sessions: acc.sessions + (p.sessions ?? 0),
      incomplete: acc.incomplete + (p.incomplete ?? 0),
      lowConfidenceDropped: acc.lowConfidenceDropped + (p.lowConfidenceDropped ?? 0),
      threshold: acc.threshold ?? p.threshold,
    }),
    { sessions: 0, incomplete: 0, lowConfidenceDropped: 0, threshold: undefined },
  );
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

/** N15-obs8: the rows a run names that workspace isolation hides from the viewer — the id
 *  stays, the name is null and the flag is true, so the console renders a placeholder. */
export interface RunHiddenRefs {
  agent: boolean;
  dataset: boolean;
}

/** What a scoring judge actually runs with (`judge_budget.judge_inference`). A model whose
 *  catalog row rejects `temperature` (Kimi K3) samples at its provider's default:
 *  `temperature` is then the string `"provider default"` and `temperature_applied` false. */
export interface JudgeInference {
  model_id: string;
  temperature: number | "provider default";
  temperature_applied: boolean;
  max_tokens: number;
  /** N17-obs449: an insights run's FailureAnalysis judge (the `failure_detection` role) */
  failure_detection?: Omit<JudgeInference, "failure_detection">;
}

/** "global.zai.glm-5.3 · temperature 0" / "… · provider default temperature (not deterministic)" */
export function judgeInferenceSummary(
  judge: JudgeInference,
  t: (key: string, options?: Record<string, unknown>) => string,
): string {
  const temperature = judge.temperature_applied
    ? t("v2.taskDetail.judgeTemperature", { value: judge.temperature })
    : t("v2.taskDetail.judgeProviderDefault");
  return `${judge.model_id} · ${temperature}`;
}

export interface EvaluationRunInfo {
  id: string;
  agent_id: string;
  /** null when `hidden.agent` (the viewer may not see the agent) */
  agent_name: string | null;
  /** absent on older backends — nothing masked */
  hidden?: RunHiddenRefs;
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
  /** evaluator records (insights: sessions) that errored on the worker; null when none
   *  did — all errored makes the run `failed`, some keep it `completed` with `error` set */
  errors?: EvaluationRecordErrors | null;
  /** N12-obs47: the sessions a partially failed evaluators run left unscored, and why;
   *  null on a clean run. Absent on older backends / rows finished before it existed
   *  (reading the task detail or “Read failure details” backfills those). */
  session_failures?: EvaluationSessionFailures | null;
  /** Eval accuracy item 1: the extraction reconciliation of the evaluated sessions — each
   *  turn's extracted answer (what the judges scored) against the collected replay answer
   *  or the chat ledger. Null when the worker reported none; absent on older backends. */
  extraction_check?: ExtractionCheckSummary | null;
  /** R3-f: expected vs judged units (no silent failure); null on insights runs / older rows */
  unit_coverage?: UnitCoverage | null;
  /** P8.3 / P8.4: the run's judge tokens and token-budget verdict; null when unreported */
  token_usage?: RunTokenUsage | null;
  /** N17-obs418: the newest worker heartbeat (`status.json` updated_at, ISO UTC) the poller
   *  read while the run's jobs ran; null before the first poll, absent on older backends. */
  last_heartbeat_at?: string | null;
  /** N17-obs450 (additive): `error` as a stable reason (`run_failure.classify`) — the
   *  console words it (`v2/runFailure.ts`), the raw text stays in `error` */
  error_code?: string | null;
  error_params?: Record<string, unknown> | null;
  /** P7.1: the evaluator versions the run was submitted with ([] on older rows) */
  evaluator_versions?: { id: string; name: string | null; kind: string; version: number | string | null;
    definition_hash: string | null }[];
  /** P7.6: the evaluation worker release the run was dispatched to */
  worker_release?: string | null;
  /** P3.2 (N17-obs350): the judge of the run's built-in evaluators (a custom judge keeps its
   *  own model); null on older rows, absent on older backends. */
  judge_model_id?: string | null;
  /** P3.1 (N17-obs349): the judge's effective inference config; null on older rows. */
  judge_inference?: JudgeInference | null;
  /** N17-obs407: expected-trajectory names the run start rewrote to the agent's own. */
  tool_name_rewrites?: { scenario_id: string; from: string; to: string }[];
  /** N17-obs407: non-blocking warnings — an expected tool that never appears in the run's
   *  spans when the agent's tool names could not be checked up front. */
  warnings?: { kind: string; session_id: string; scenario_id: string | null; tools: string[]; message: string }[];
  /** D3-d: the dataset split the run replayed (null for passive scopes / older rows). */
  dataset_split?: "all" | "tuning" | "held_out" | null;
  /** D3-c: the local dataset revision the run replayed (null for cloud / passive / older) */
  dataset_revision?: number | null;
  error: string | null;
  /** The agent version the run evaluated (`#seq`, and whether it was stamped at
   *  submit, inferred for an older row, or observed for a passive scope); null for a
   *  log-source run or an agent with no deployed snapshot. Absent on older backends. */
  snapshot?: RunSnapshotRef | null;
  created_at?: string | null;
  /** operator-facing task name/description (console V2); null on unnamed runs */
  name?: string | null;
  description?: string | null;
  updated_at?: string | null;
  /** `users.id` of the creating account (the ownership key); null for the config
   *  admin / auth-disabled operator, system-created and legacy rows. */
  created_by?: string | null;
  /** Creator's username at creation time (display only); null when unknown —
   *  legacy rows and resources created outside the console. */
  created_by_name?: string | null;
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

/** `telemetry_incomplete`: the session answered but its spans never became queryable
 *  (re-evaluating cannot recover it); `replay_failed`: the target never answered the
 *  scenario (a retry may help); `evaluator_error`: a judge failed on a session it did see. */
export type SessionFailureKind = "telemetry_incomplete" | "replay_failed" | "evaluator_error";

export interface EvaluationSessionFailures {
  total: number;
  failed: number;
  kind: SessionFailureKind | "mixed";
  /** failed sessions in dataset order; `index` = 1-based scenario position; `arm` (N12-obs62,
   *  additive) = the session's pre-assigned experiment / canary arm (`C` | `T1`), null on a
   *  plain run and absent on older backends */
  sessions: { session_id: string; index: number | null; kind: SessionFailureKind; error_type: string; message: string; arm?: string | null }[];
}

export interface RunCoverage {
  total: number;
  scored: number;
  failed: number;
  kind: EvaluationSessionFailures["kind"] | "unknown";
  /** 1-based scenario positions that were not (fully) scored (sorted) */
  excluded: number[];
  /** N12-obs62: the experiment / canary arm of each excluded position that has one
   *  (`C` | `T1`), keyed by position; empty on a plain run and on legacy rows */
  arms: Record<number, string>;
  firstError: string | null;
  /** finished before the per-session summary existed: only the record counts are known */
  legacy: boolean;
  /** the N12-bug4 record counts a legacy row carries */
  records: { count: number; total: number } | null;
}

/** How much of a partially failed run was scored — null for a run with no error, or
 *  one that is not a completed run with errored records. A row from before the
 *  per-session summary (`errors` but no `session_failures`) reads as `legacy`. */
export function runCoverage(
  run: Pick<EvaluationRunInfo, "status" | "error" | "errors" | "session_failures" | "session_ids">,
): RunCoverage | null {
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
      arms: Object.fromEntries(
        f.sessions.filter((s) => s.index != null && s.arm).map((s) => [s.index as number, s.arm as string]),
      ),
      firstError: first ? `${first.error_type}: ${first.message}` : null,
      legacy: false,
      records: null,
    };
  }
  if (!run.errors || run.errors.count <= 0) return null;
  const total = run.session_ids.length;
  return {
    total,
    failed: 0,
    scored: total,
    kind: "unknown",
    excluded: [],
    arms: {},
    firstError: `${run.errors.first_type}: ${run.errors.first_message}`,
    legacy: true,
    records: { count: run.errors.count, total: run.errors.total },
  };
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

export type RecommendableReason = "evaluator_error" | "replay_failed" | "low_coverage" | "legacy" | "unknown" | "not_completed";

/** Whether a run may seed AI recommendations in the console: a clean completed run with a
 *  job, or one whose only losses are telemetry gaps and that still scored at least half of
 *  its sessions (the backend's `eligibility` accepts any completed run with results; this
 *  is the console's stricter default pick). */
export function runRecommendable(run: EvaluationRunInfo): { ok: boolean; reason?: RecommendableReason; coverage: RunCoverage | null } {
  if (run.status !== "completed" || !run.batch_eval_id) return { ok: false, reason: "not_completed", coverage: null };
  const coverage = runCoverage(run);
  if (!run.error?.trim()) return { ok: true, coverage: null };
  // a pre-summary row: “Read failure details” (re-check) can still qualify it
  if (coverage?.legacy) return { ok: false, reason: "legacy", coverage };
  if (!coverage || coverage.kind === "unknown") return { ok: false, reason: "unknown", coverage };
  if (coverage.kind === "replay_failed") return { ok: false, reason: "replay_failed", coverage };
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
export interface EvaluationRecordErrors {
  count: number;
  total: number;
  first_type: string;
  first_message: string;
}

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
  /** N17-obs317: the turn the row judged; the call a TOOL_CALL row judged */
  trace_id?: string | null;
  span_id?: string | null;
  tool_name?: string | null;
  /** that turn's extraction reconciliation */
  extraction_status?: ExtractionCheckStatus | null;
  /** the judge that produced the row (same names as the run; `judge` is their alias) */
  judge_model_id?: string | null;
  judge_inference?: JudgeInference | null;
  /** N17-obs391: the dataset scenario the session replayed (label = its input preview) */
  scenario_id?: string | null;
  scenario_label?: string | null;
  /** R3-f: why the row did not score; null on a judged row */
  failure_class?: FailureClass | null;
}

/** R3-f: why a unit was not scored (`run_coverage.FAILURE_CLASSES`). */
export type FailureClass =
  | "timeout" | "throttling" | "context_overflow" | "truncation" | "failed_turn"
  | "infrastructure" | "evaluator_error";

export const FAILURE_CLASSES: readonly FailureClass[] = [
  "timeout", "throttling", "context_overflow", "truncation", "failed_turn", "infrastructure",
  "evaluator_error",
];

/** P8.3 / P8.4: the run's judge tokens (`service.fold_token_usage`). */
export interface RunTokenUsage {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  budget: number | null;
  exhausted: boolean;
}

/** R3-f: a run's unit coverage (`run_coverage.run_coverage`). */
export interface UnitCoverage {
  expected: number;
  judged: number;
  missing: number;
  coverage: number;
  threshold: number;
  below_threshold: boolean;
  failures: Partial<Record<FailureClass, number>>;
  /** N17-obs414: turns the AGENT failed (never judged) — outside the ratio; absent on older runs */
  agent_failures?: number;
  evaluators: {
    evaluator_id: string; level: string; expected: number; judged: number; missing: number;
    failures: Partial<Record<FailureClass, number>>;
  }[];
}

export interface EvaluationRunSessionResults {
  session_id: string;
  /** N17-obs391: the dataset scenario this session replayed */
  scenario_id?: string | null;
  scenario_label?: string | null;
  results: EvaluationRunResultRow[];
  /** this session's extraction reconciliation; null / absent when none was reported */
  extraction_check?: ExtractionCheck | null;
}

export type ExtractionCheckStatus = "ok" | "mismatch" | "unverifiable";

/** Reference-free anomaly counters over the extracted answers. */
export interface ExtractionAnomalies {
  turns: number;
  empty: number;
  tool_json_like: number;
  repeated_previous: number;
}

export interface ExtractionMismatch {
  /** 1-based turn; a turn missing on one side reads `(missing)` */
  turn: number;
  expected_head: string;
  extracted_head: string;
}

/** One session's reconciliation (`extraction_check.check`): `source` is `replay` (the
 *  worker's collected answer), `chat_ledger` (the platform chat) or `runtime_logs` (the
 *  session's own runtime records, N17-obs316), null when unverifiable. */
export interface ExtractionCheck {
  status: ExtractionCheckStatus;
  source: "replay" | "chat_ledger" | "runtime_logs" | null;
  turns_checked: number;
  mismatches: ExtractionMismatch[];
  mismatch_count: number;
  anomalies: ExtractionAnomalies;
}

/** The run- / window-level summary (`extraction_check.summarise`). */
export interface ExtractionCheckSummary {
  status: ExtractionCheckStatus;
  sessions: number;
  ok: number;
  mismatch: number;
  unverifiable: number;
  turns_checked: number;
  mismatched_sessions: string[];
  anomalies: ExtractionAnomalies & { empty_rate: number };
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
  /** the run-level extraction reconciliation summary (same as the run payload's) */
  extraction_check?: ExtractionCheckSummary | null;
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
  /** CN additive (N12-obs44): the backend's own accept predicate — harness and zip_runtime
   *  agents take their prompt from the spec; absent on a pre-obs44 backend */
  accept_eligible?: boolean;
  accept_ineligible_reason?: "agent_missing" | "method" | "system_preset" | "converted_locked" | null;
}

/** N17-obs464: one manual edit published on top of an accepted recommendation. */
export interface RecommendationEdit {
  by: string;
  at: string;
  version_before: string | null;
  job_id: string;
  deployment_id: string;
  system_prompt: string;
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
  /** N17-obs453 (additive): the evaluator the provider actually optimised toward — null for a
   *  provider that takes no target (gepa_lite reads every evaluator of the run); absent on an
   *  older server (the card then falls back to `evaluator`) */
  optimized_toward?: string | null;
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
    /** reviewed red-team (`adversarial`) sessions Launchpad left out of the optimizer's
     *  evidence before the job was submitted (`recommendations.adversarial_sessions`) */
    excluded_sessions?: { session_id: string; scenario_id: string }[];
    /** D3-d: the held-out scenarios the tuning paths left out (never silent) */
    held_out_excluded?: { sessions: number; scenarios: number; message: string };
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
    /** N17-obs464: the text the acceptance published (absent on older rows) */
    system_prompt?: string | null;
    edits?: RecommendationEdit[];
  } | null;
  /** N17-obs464 (additive): manual edits published on top of the acceptance, newest last */
  edits?: RecommendationEdit[];
  /** N17-obs464 (additive): the text a manual edit starts from — the last edit, else what the
   *  acceptance published, else the AI-recommended prompt */
  final_prompt?: string | null;
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

export const CLOUD_VALUE_PREFIX = "cloud:";
export const SIMULATED_SCHEMA = "AGENTCORE_EVALUATION_SIMULATED_V1";
export const DEFAULT_EVALUATORS = ["Builtin.Correctness", "Builtin.Helpfulness"];

/** Actor-simulator models: the CN catalog (GLM 5.3 default, Kimi K3) — constraint C3. */
export const ACTOR_MODELS: string[] = ALLOWED_MODEL_IDS; // live: grows with /api/capabilities (N-R6)
void MODEL_CATALOG;

export const ACTIVE_RUN_STATUSES = new Set([
  "queued",
  "invoking",
  "waiting",
  "evaluating",
]);

/**
 * The recommendation providers a picker may offer (N12-obs43): only those the
 * workspace has enabled — `GET /api/eval/recommendation-providers` reports `enabled=false`
 * for `harness_optimizer` until the operator opts in, and the backend refuses a disabled
 * pick with 422 `recommendation.provider_disabled`.
 * Older servers omit the flag: treated as enabled.
 */
export function enabledProviders(providers: RecommendProviderInfo[]): RecommendProviderInfo[] {
  return providers.filter((p) => p.enabled !== false);
}

/**
 * The short name of a recommendation provider for a sentence ("GEPA-lite reads this
 * run's traces") — the registry label up to its first ` · ` separator, which carries the
 * method and the executor ("GEPA-lite · single reflective round (evaluation worker)").
 * N12-obs32: the console names the provider that produced a recommendation from the
 * registry and never the Global managed service.
 */
export function providerDisplayName(label: string): string {
  const head = label.split(" · ")[0]?.trim();
  return head || label.trim();
}

/**
 * The display name for a stored recommendation's `result.provider` id, resolved through
 * the provider list the server advertises; falls back to the id itself when the
 * provider is unknown (switched off since, or an older row).
 */
export function providerLabelFor(providers: RecommendProviderInfo[], providerId: string): string {
  const match = providers.find((p) => p.id === providerId);
  return match ? providerDisplayName(match.label) : providerId;
}

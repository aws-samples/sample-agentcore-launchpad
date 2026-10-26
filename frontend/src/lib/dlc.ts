/**
 * Agent-DLC wire types — the criteria table (判据), the golden set, judge
 * calibration, the release gate, waivers, admission and the drift watch.
 * See `docs/agent-dlc-design.md`; the client lives in `api.ts` as `dlcApi`.
 *
 * Shapes mirror `backend/app/routers/dlc.py` and the `*_out` helpers in
 * `backend/app/dlc/`. Anything the backend can return as null is `| null` here
 * rather than optional, so a missing number stays visible instead of silently
 * reading as zero.
 */

export const DLC_DIMENSIONS = [
  "cognition",
  "quality",
  "responsibility",
  "cost",
  "performance",
] as const;
export type DlcDimension = (typeof DLC_DIMENSIONS)[number];

export const DLC_TIERS = ["redline", "gate", "observe"] as const;
export type DlcTier = (typeof DLC_TIERS)[number];

export const CASE_TIERS = ["known_good", "known_bad", "ambiguous", "adversarial"] as const;
export type CaseTier = (typeof CASE_TIERS)[number];

export type GoldenSplit = "dev" | "regression" | "holdout";
/** Admission and the item editor never write the holdout; only initial curation does. */
export type WritableSplit = "dev" | "regression";

export type ExecutorKind = "evaluator" | "metric" | "manual";
export type EvaluatorKind = "code" | "trajectory" | "judge";

export interface CriterionExecutor {
  kind?: ExecutorKind;
  evaluator_id?: string;
  evaluator_kind?: EvaluatorKind;
  [key: string]: unknown;
}

export interface MetricRule {
  metric?: string;
  op?: "<=" | ">=";
  value?: number;
}

export interface PassKRule {
  /** `k` sessions of the same scenario; `mode` decides what counts as a pass. */
  k?: number;
  mode?: "all" | "any" | "majority";
}

export interface CriterionExample {
  label?: string;
  text?: string;
  [key: string]: unknown;
}

/** One row of the criteria table — the methodology's atomic unit. */
export interface Criterion {
  key: string;
  position: number;
  text: string;
  dimension: DlcDimension;
  tier: DlcTier;
  threshold: number | null;
  metric_rule: MetricRule | null;
  level: "session" | "trace" | "tool_call";
  executor: CriterionExecutor;
  denominator: "sessions" | "turns" | "fields";
  expected_type: string;
  pass_k: PassKRule | null;
  attribution_layer: string | null;
  owner: string;
  examples: CriterionExample[];
  notes: string;
  origin: string;
  online: boolean;
  is_judge: boolean;
  /** A judge criterion is demoted to `observe` until it is calibrated. */
  effective_tier: DlcTier;
  calibrated: boolean | null;
  calibration?: CalibrationStatus | null;
}

/** What the editor sends; the server owns position, origin and the derived fields. */
export interface CriterionInput {
  key: string;
  text: string;
  dimension: DlcDimension;
  tier: DlcTier;
  threshold?: number | null;
  metric_rule?: MetricRule | null;
  level?: "session" | "trace" | "tool_call";
  executor?: CriterionExecutor;
  denominator?: "sessions" | "turns" | "fields";
  expected_type?: string;
  pass_k?: PassKRule | null;
  attribution_layer?: string | null;
  owner?: string;
  examples?: CriterionExample[];
  notes?: string;
}

export interface CriteriaSet {
  id: string;
  lineage_id: string;
  kind: "template" | "agent";
  agent_id: string | null;
  template_id: string | null;
  template_version: number | null;
  name: string;
  description: string;
  scenario: string;
  version: number;
  status: "draft" | "published";
  parent_version: number | null;
  source: string;
  signed_by: string | null;
  signed_at: string | null;
  sign_note: string;
  removals: CriteriaRemoval[];
  published_at: string | null;
  created_by: string;
  updated_by: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface CriteriaRemoval {
  key: string;
  reason?: string;
  by?: string;
  at?: string;
}

export interface CriteriaSummary {
  count: number;
  tiers: Record<DlcTier, number>;
  judge_count: number;
  judge_share: number;
  run_cadence: "full_every_commit" | "split_quick_full" | "split_criteria";
  declared_gates: number;
  effective_gates: number;
  /** Every gate passing independently: 0.95^8 is 66%, which is the point. */
  compound_gate_rate: number | null;
}

export interface CriteriaFinding {
  level: "error" | "warning";
  code: string;
  message: string;
  key?: string;
}

export interface CalibrationPolicy {
  period_days: number;
  kappa_floor: number;
}

export interface CalibrationStatus {
  calibrated: boolean;
  reason?: string;
  record_id?: string;
  judge_human_kappa?: number | null;
  expires_at?: string;
  expired_at?: string;
}

export interface CriteriaSetVersionRef {
  version: number;
  status: string;
  signed_by: string | null;
  published_at: string | null;
}

export interface CriteriaSetPayload {
  set: CriteriaSet;
  criteria: Criterion[];
  summary: CriteriaSummary;
  findings: CriteriaFinding[];
  calibration_policy: CalibrationPolicy;
  newer_template_version: number | null;
  versions: CriteriaSetVersionRef[];
}

export interface CriteriaDiffChange {
  key: string;
  change: "added" | "removed" | "changed";
  fields?: string[];
  before?: Record<string, unknown>;
  after?: Record<string, unknown>;
}

export interface CriteriaDiff {
  from: CriteriaSet;
  to: CriteriaSet;
  changes: CriteriaDiffChange[];
}

/* ── golden sets ─────────────────────────────────────────────────────────── */

export interface GoldenItemMeta {
  case_tier?: CaseTier;
  criteria_ids?: string[];
  origin?: string;
  expected_source?: "annotator" | "consensus" | "adjudicated" | "agent_observed";
  retired?: boolean;
  retired_reason?: string;
  source_session?: string | null;
  cluster_id?: string | null;
  fault_category?: string | null;
  admitted_from?: string;
  added_by?: string;
  added_at?: string;
  [key: string]: unknown;
}

export interface GoldenItem {
  scenario_id: string;
  turns: { input?: unknown; expected_response?: string }[];
  metadata?: { dlc?: GoldenItemMeta };
  /** Only on seeding input: which split this item belongs to. */
  split?: GoldenSplit;
}

export interface GoldenSplitInfo {
  id: string;
  split: GoldenSplit;
  name: string;
  items: number;
  active_items: number;
  retired_items: number;
  cloud_dataset_id: string | null;
  draft_status: string | null;
  versions: { version: number | string; [key: string]: unknown }[];
  latest_version: string | null;
}

export interface CoverageRow {
  key: string;
  known_good: number;
  known_bad: number;
  ambiguous: number;
  adversarial: number;
  total: number;
  /** Fewer than three items: the rate this criterion reports is noise. */
  thin: boolean;
}

export interface GoldenCoverage {
  criteria: CoverageRow[];
  items: number;
  unmapped_items: number;
  /** Items whose expected answer is the agent's own output — not a standard. */
  agent_observed_items: number;
  sources: { origin: string; count: number; bias: string }[];
}

export interface GoldenSet {
  id: string;
  name: string;
  description: string;
  criteria_lineage_id: string | null;
  created_at: string | null;
  splits: Partial<Record<GoldenSplit, GoldenSplitInfo>>;
  coverage?: GoldenCoverage;
}

/* ── annotation + calibration ────────────────────────────────────────────── */

export interface AnnotationLabel {
  annotator: string;
  label: string;
  answer: string;
  rationale: string;
  adjudicated: boolean;
}

export interface AnnotationItem {
  ref: string;
  session_id?: string;
  scenario_id?: string;
  /** The user's question and the reference answer, from the run's dataset. */
  input?: string;
  expected?: string;
  /** What the agent actually said in this session. */
  answer?: string;
  /** Hidden from annotators until the task closes — labelling is blind. */
  judge_label?: string;
  judge_explanation?: string;
  my_label: string | null;
  my_answer: string | null;
  my_rationale: string | null;
  labels?: AnnotationLabel[];
}

export interface AnnotationTask {
  id: string;
  agent_id: string | null;
  criteria_set_id: string | null;
  criterion_key: string;
  purpose: "judge_calibration" | "golden_answer" | "admission";
  status: "open" | "adjudicating" | "closed";
  annotators: string[];
  adjudicator: string | null;
  items: AnnotationItem[];
  progress: Record<string, number>;
  total: number;
  created_by: string;
  created_at: string | null;
  closed_at: string | null;
  run_id: string | null;
}

/** An account-free labelling credential; `annotator` is the identity its votes carry. */
export interface AnnotationLink {
  id: string;
  annotator: string;
  label: string;
  prefix: string;
  state: "active" | "revoked" | "disabled" | "expired";
  created_by: string;
  expires_at: string | null;
  last_used_at: string | null;
  use_count: number;
}

export interface Disagreement {
  ref: string;
  human: string | null;
  judge: string | null;
  votes: Record<string, string>;
  judge_explanation?: string | null;
  input?: string | null;
  answer?: string | null;
  needs_adjudication?: boolean;
}

export interface Agreement {
  task_id: string;
  criterion_key: string;
  n: number;
  pairs: number;
  human_human_kappa: number | null;
  judge_human_kappa: number | null;
  kappa_ci: [number, number] | null;
  band: string | null;
  human_band: string | null;
  confusion: Record<string, number>;
  disagreements: Disagreement[];
  accuracy: number | null;
  policy: CalibrationPolicy;
  suggested_verdict: "aligned" | "not_aligned" | "insufficient_n" | "one_class";
}

export interface CalibrationRecord {
  id: string;
  criterion_key: string;
  criteria_set_version: number | null;
  evaluator_id: string;
  evaluator_updated_at: string | null;
  task_id: string | null;
  n: number;
  human_human_kappa: number | null;
  judge_human_kappa: number | null;
  kappa_ci: [number | null, number | null];
  band: string | null;
  confusion: Record<string, number>;
  disagreements: Disagreement[];
  verdict: "aligned" | "not_aligned";
  decided_by: string;
  decided_at: string | null;
  note: string;
}

export interface CalibrationHistory {
  criterion_key: string;
  records: CalibrationRecord[];
  status: CalibrationStatus;
  policy: CalibrationPolicy;
}

/* ── the gate and the release ────────────────────────────────────────────── */

export type GateVerdict = "PASS" | "BLOCKED" | "INVALID";
export type GateRowVerdict = "PASS" | "BLOCKED" | "INVALID" | "WAIVED" | "OBSERVED";

export interface GateRow {
  key: string;
  text: string;
  dimension: DlcDimension;
  tier: DlcTier;
  effective_tier: DlcTier;
  verdict: GateRowVerdict;
  reason?: string | null;
  measured?: number | null;
  threshold?: number | null;
  violations?: number;
  n?: number | null;
  expected?: number | null;
  missing?: number | null;
  undetermined_rate?: number | null;
  wilson_low?: number | null;
  wilson_high?: number | null;
  /** The threshold sits inside the confidence interval — the sample cannot decide. */
  threshold_inside_ci?: boolean;
  trend?: number | null;
  waiver?: {
    id: string;
    expires_on: string;
    risk_owner: string;
    approved_by: string | null;
  } | null;
  [key: string]: unknown;
}

/** A metric criterion's measured value in its own unit, against its bound. */
export function metricValueLabel(row: { metric_rule?: unknown; value?: unknown; measured?: number | null }): string {
  const rule = (row.metric_rule ?? {}) as { metric?: string; op?: string; value?: number };
  const raw = typeof row.value === "number" ? row.value : typeof row.measured === "number" ? row.measured : null;
  const usd = rule.metric === "cost_per_success_usd";
  const fmt = (v: number) => (usd ? `$${v.toFixed(4)}` : Math.round(v).toLocaleString("en-US"));
  const unit = rule.metric?.endsWith("_ms") ? " ms" : "";
  const bound = typeof rule.value === "number" ? ` (${rule.op ?? "<="} ${fmt(rule.value)})` : "";
  return raw === null ? "—" : `${fmt(raw)}${unit}${bound}`;
}

/** A waiver's "measured / required" cell. A metric criterion (latency, tokens,
 * cost) is shown in its own unit against its bound, a rate as percentages. */
export function waiverGapLabel(
  waiver: { criterion_key: string; actual: number | null; threshold: number | null },
  rows: { key: string; kind?: unknown; metric_rule?: unknown }[],
): string {
  if (waiver.actual === null) return "—";
  const metricRow = rows.find((r) => r.key === waiver.criterion_key && r.kind === "metric");
  if (metricRow) return metricValueLabel({ metric_rule: metricRow.metric_rule, value: waiver.actual });
  // no report row to read the rule from: a value above 1 cannot be a rate
  if (waiver.actual > 1) return Math.round(waiver.actual).toLocaleString("en-US");
  if (waiver.threshold === null) return "—";
  const pctOf = (v: number, digits: number) => `${(v * 100).toFixed(digits)}%`;
  return `${pctOf(waiver.actual, 1)} / ${pctOf(waiver.threshold, 0)}`;
}

/** The estimated-cost figure: a dash while the estimate is still loading (it used to
 * read "unpriced" for the first second of every page load), the unknown label only
 * when the server answered that it cannot price the run. */
export function estimateValue(
  est: { total_usd?: number | null } | null | undefined,
  unknownLabel: string,
): string {
  if (!est) return "—";
  return est.total_usd === null || est.total_usd === undefined ? unknownLabel : `$${est.total_usd.toFixed(2)}`;
}

/** What a release froze per golden split: `releases.py` writes the split's
 * dataset id, its version label (null until the split is versioned) and the
 * number of active items. Older records may hold a bare label. */
export interface GoldenSplitVersion {
  dataset_id: string;
  version: string | null;
  items: number;
}

export type GoldenVersions = Record<string, GoldenSplitVersion | string | null>;

export interface GateProvenance {
  criteria_set_version: number | null;
  criteria_signed_by: string | null;
  golden_versions: GoldenVersions;
  evaluator_set_hash: string | null;
  candidate_version: string | null;
  previous_live_version: string | null;
  issues: string[];
}

export interface GateReport {
  verdict: GateVerdict;
  decided_at: string;
  criteria: GateRow[];
  redline_violations: string[];
  invalid: string[];
  gate_failures: string[];
  waived: string[];
  provenance: GateProvenance;
  /** The fixed order the four gates are applied in. */
  order: string[];
  runs?: { id: string; split: string | null; status: string; denominator: unknown }[];
}

export interface ReleaseRecord {
  id: string;
  agent_id: string;
  promotion_id: string | null;
  candidate_version: string | null;
  candidate_endpoint: string | null;
  previous_live_version: string | null;
  criteria_set_id: string | null;
  criteria_set_version: number | null;
  golden_versions: GoldenVersions;
  evaluator_set_hash: string | null;
  run_ids: string[];
  gate_report: GateReport | Record<string, never>;
  decision: "open" | "pending" | "evaluating" | "blocked" | "invalid" | "released" | "rolled_back" | "superseded";
  requested_by: string;
  decided_by: string | null;
  decided_at: string | null;
  note: string;
  waiver_ids: string[];
  rollback_target: Record<string, unknown>;
  attachments: unknown[];
  created_at: string | null;
}

export interface LiveState {
  endpoint_mode: "default" | "live";
  ledger_version: string | null;
  gateable: boolean;
  gateable_reason: string | null;
  live_version?: string | null;
  candidate_version?: string | null;
  error?: string;
}

export interface Waiver {
  id: string;
  agent_id: string;
  criterion_key: string;
  criteria_set_version: number | null;
  actual: number | null;
  threshold: number | null;
  reason: string;
  risk_owner: string;
  compensating_control: string;
  expires_on: string | null;
  expired: boolean;
  active: boolean;
  status: "requested" | "approved" | "rejected" | "revoked" | "expired";
  requested_by: string;
  approved_by: string | null;
  approved_at: string | null;
  note: string;
  /** How often this criterion has been waived — a repeat waiver is a standard problem. */
  times_waived: number | null;
  created_at: string | null;
}

export interface ReleaseState {
  agent_id: string;
  release_mode: "direct" | "gated";
  state: LiveState;
  criteria_set: CriteriaSet | null;
  pending: ReleaseRecord | null;
  records: ReleaseRecord[];
  waivers: Waiver[];
}

export interface GateResponse {
  status?: "decided" | "evaluating";
  report?: GateReport;
  runs?: { id: string; status: string }[];
  record: ReleaseRecord;
}

/* ── admission ───────────────────────────────────────────────────────────── */

export interface Redaction {
  status: "clean" | "redacted" | "blocked" | "unavailable";
  reason?: string;
  entities: string[];
  question?: string;
  answer?: string;
}

export interface NearestItem {
  dataset_id: string;
  split: GoldenSplit;
  scenario_id: string;
  input: string;
  match: "exact" | "normalized";
}

export interface AdmissionCandidate {
  id: string;
  agent_id: string;
  source: string;
  source_ref: string;
  session_id: string | null;
  cluster_id: string | null;
  cluster_name: string | null;
  affected_sessions: number | null;
  fault_category: string | null;
  question: string;
  answer: string;
  proposed_criteria: string[];
  existing_judgement: Record<string, string>;
  /** The evaluators called this a pass — a new case and a calibration sample at once. */
  judged_wrong: boolean;
  duplicate_of: string | null;
  redaction: Redaction | Record<string, never>;
  status: "new" | "annotating" | "admitted" | "rejected" | "duplicate";
  decided_by: string | null;
  decided_at: string | null;
  note: string;
  admitted_item_ref: string | null;
  admitted_dataset_id: string | null;
  created_at: string | null;
  nearest?: NearestItem[];
}

export interface AdmissionQueue {
  candidates: AdmissionCandidate[];
  priority: string[];
  clusters: {
    cluster_id: string;
    name: string | null;
    affected_sessions: number | null;
    candidates: number;
    noise: number;
  }[];
  counts: Record<string, number>;
}

/* ── watch and drift ─────────────────────────────────────────────────────── */

export interface WatchConfig {
  id: string;
  agent_id: string;
  criteria_set_id: string | null;
  dataset_id: string | null;
  every: "daily" | "weekly";
  at_hour: number;
  tz: string;
  repeats: number;
  max_cost_usd: number | null;
  enabled: boolean;
  last_run_id: string | null;
  last_checked_at: string | null;
  next_due_at: string | null;
  last_status: string | null;
  last_detail: string | null;
}

export interface DimensionPoint {
  run_id: string;
  at: string | null;
  rate: number;
  agent_version: string | null;
  criteria_set_version: number | null;
  baseline: number | null;
  baseline_points: number;
}

export interface WatchAlert {
  family: "count" | "score" | "distribution";
  severity: "page" | "digest";
  detail: string;
  criterion_key?: string;
  dimension?: string;
  rate?: number;
  baseline?: number;
  tvd?: number;
}

export interface WatchAlerts {
  checked_at: string;
  runs_considered: number;
  count: WatchAlert[];
  score: WatchAlert[];
  distribution: WatchAlert[];
  firing: number;
  /** Silence is an explicit answer, so a dead monitor cannot read as a quiet week. */
  quiet: boolean;
  baseline_ready: Record<string, boolean>;
}

export interface DriftTriage {
  judges: {
    criterion_key: string;
    evaluator_id: string | null;
    calibrated: boolean;
    reason?: string;
    judge_human_kappa: number | null;
    task_id: string | null;
    recalibrate: boolean;
  }[];
  needs_recalibration: string[];
  agent_versions_in_window: string[];
  versions_changed: boolean;
  hint: string;
}

export interface WatchView {
  config: WatchConfig | null;
  series: Record<string, DimensionPoint[]>;
  alerts: WatchAlerts;
  drift: DriftTriage;
  runs: {
    id: string;
    at: string | null;
    split: string | null;
    agent_version: string | null;
    criteria_set_version: number | null;
  }[];
}

/* ── run results, comparison, cost ───────────────────────────────────────── */

export interface CriterionSummaryEntry {
  key: string;
  dimension: DlcDimension;
  tier: DlcTier;
  kind: ExecutorKind | null;
  n?: number;
  expected?: number;
  pass?: number;
  fail?: number;
  inconclusive?: number;
  error?: number;
  rate?: number | null;
  wilson_low?: number | null;
  wilson_high?: number | null;
  missing?: number;
  undetermined_rate?: number | null;
  pass_k?: { pass_k: number | null; scenarios: number; mode: string } | null;
  /** Metric criteria report a value against a rule, not a rate. */
  value?: number | null;
  rule?: MetricRule;
  verdict?: string;
}

export interface CriterionResult {
  criterion_key: string;
  scenario_id: string;
  attempt: number;
  session_id: string;
  evaluator_id: string | null;
  level: string;
  unit_ref: string | null;
  raw_value: number | null;
  raw_label: string | null;
  explanation: string;
  verdict: "pass" | "fail" | "inconclusive" | "error";
  error_code: string | null;
}

export interface RunCriteria {
  run_id: string;
  criteria_set_id: string | null;
  criteria_set_version: number | null;
  split: string | null;
  repeats: number;
  agent_version: string | null;
  endpoint_qualifier: string | null;
  summary: Record<string, CriterionSummaryEntry>;
  denominator: Record<string, number>;
  cost_estimate: CostEstimate | Record<string, never>;
  cost_actual: Record<string, number> | Record<string, never>;
  results: CriterionResult[];
  truncated: boolean;
}

export interface LadderRung {
  run_id: string;
  name: string | null;
  status: string;
  agent_version: string | null;
  criteria_set_version: number | null;
  split: string | null;
  dataset_version: string | null;
  repeats: number;
  created_at: string | null;
  mean_gate_rate: number | null;
  redline_violations: number;
  cost_actual_usd: number | null;
  /** eval-cost §1.3: the rung's measured parts (null while unmeasured) and its estimate */
  cost?: RungCost;
  /** Which of the seven spec layers changed since the previous rung. */
  layers_changed: string[];
}

export type RunCostPartKey = "agent_usd" | "runtime_usd" | "judge_usd" | "managed_evaluations_usd";

export interface RungCost extends Partial<Record<RunCostPartKey, number | null>> {
  total_usd: number | null;
  estimate_usd: number | null;
  source: string | null;
}

/** What the last run cost against the first (eval-cost §1.3 compare page). */
export interface CostDelta {
  before_usd: number | null;
  after_usd: number | null;
  delta_usd: number | null;
  delta_pct: number | null;
  parts: Partial<Record<RunCostPartKey, number | null>>;
}

export interface ComparedCriterion {
  key: string;
  dimension: DlcDimension | null;
  tier: DlcTier | null;
  threshold: number | null;
  before: { pass: number; n: number; rate: number | null };
  after: { pass: number; n: number; rate: number | null };
  delta?: number;
  p_value?: number | null;
  significant?: boolean;
}

export interface ScenarioMove {
  criterion_key: string;
  scenario_id: string;
  before: string | null;
  after: string;
}

export interface TwoNumbers {
  shared_criteria: string[];
  added_criteria: string[];
  removed_criteria: string[];
  baseline_under_old: { rate: number | null; n: number; criteria_set_version: number | null };
  current_under_old: { rate: number | null; n: number };
  current_under_new: { rate: number | null; n: number; criteria_set_version: number | null };
  agent_delta: number | null;
  standard_delta: number | null;
}

export interface RunComparison {
  runs: LadderRung[];
  comparable: boolean;
  criteria: ComparedCriterion[];
  fixed: ScenarioMove[];
  new_failures: ScenarioMove[];
  still_failing: ScenarioMove[];
  splits: string[];
  incomparable_reason?: string;
  two_numbers?: TwoNumbers;
  warning?: string;
  /** absent on a ladder with fewer than two runs (and from an older server) */
  cost_delta?: CostDelta;
}

export interface JudgeCostDetail {
  evaluator_id: string;
  /** agentcore_evaluations (service capacity, no dollar figure) | your_account */
  billed_by?: string;
  model?: string | null;
  usd?: number | null;
  note?: string;
  [key: string]: unknown;
}

export interface CostEstimate {
  items: number;
  repeats: number;
  sessions: number;
  agent_usd: number | null;
  /** How the per-session figure was obtained: history_7d | rough | unavailable. */
  agent_basis: string;
  judge_usd: number | null;
  judges: JudgeCostDetail[];
  total_usd: number | null;
  estimate: true;
  duration_minutes: number | null;
  confirm_required: boolean;
  confirm_usd: number | null;
  over_limit: boolean;
  max_usd: number | null;
  unpriced: boolean;
}

/** Judges this account pays for whose model has no price: their cost is not in the total. */
export function unpricedJudges(estimate: CostEstimate | null | undefined): JudgeCostDetail[] {
  return (estimate?.judges ?? []).filter((j) => j.billed_by === "your_account" && (j.usd ?? null) === null);
}

/* ── scorecard and audit ─────────────────────────────────────────────────── */

export interface ScorecardDimension {
  dimension: DlcDimension;
  criteria: number;
  effective_gates: number;
  declared_gates: number;
  redlines: number;
  redline_violations: number;
  current: number | null;
  standard: number | null;
  series: DimensionPoint[];
  /** cost / performance criteria measure a value against a rule, not a pass rate */
  metrics: {
    key: string;
    metric: string | null;
    op: string;
    bound: number | null;
    value: number | null;
    verdict: string | null;
    n: number | null;
  }[];
  not_applicable: boolean;
}

export interface Scorecard {
  agent_id: string;
  agent_name: string;
  criteria_set: CriteriaSet | null;
  dimensions: ScorecardDimension[];
  last_run: { id: string; at: string } | null;
  last_gate: GateVerdict | null;
  /** What became of that gate's release — `released`, `blocked`, still waiting… */
  last_gate_decision: ReleaseRecord["decision"] | null;
  last_gate_at: string | null;
  release: LiveState;
  open_waivers: Waiver[];
  calibration_debt: { criterion_key: string; reason?: string }[];
  calibration_policy: CalibrationPolicy;
  coverage: GoldenCoverage | null;
  alerts: WatchAlerts;
}

export interface AuditEntry {
  id: string;
  actor: string;
  action: string;
  target: string;
  at: string | null;
}

/* ── pure helpers shared by the workbenches ──────────────────────────────── */

/** κ bands (Landis–Koch), used to colour an agreement number consistently. */
export function kappaTone(kappa: number | null): "green" | "blue" | "orange" | "red" | "gray" {
  if (kappa === null) return "gray";
  if (kappa >= 0.8) return "green";
  if (kappa >= 0.6) return "blue";
  if (kappa >= 0.4) return "orange";
  return "red";
}

/** Whether the pending candidate can be sent to the gate (again): never evaluated, or
 *  INVALID — a run did not complete (a backend restart, an AgentCore failure), so the
 *  evidence is incomplete and the backend accepts a fresh submit. A BLOCKED or PASS
 *  verdict is not re-run from here: it is fixed by a new candidate or a waiver. */
export function canRunGate(pending: Pick<ReleaseRecord, "decision" | "run_ids"> | null | undefined): boolean {
  if (!pending) return false;
  return (pending.run_ids ?? []).length === 0 || pending.decision === "invalid";
}

export function gateTone(
  verdict: GateRowVerdict | GateVerdict | null,
): "green" | "red" | "orange" | "gray" | "blue" {
  switch (verdict) {
    case "PASS":
      return "green";
    case "BLOCKED":
      return "red";
    case "INVALID":
      return "orange";
    case "WAIVED":
      return "blue";
    default:
      return "gray";
  }
}

export function tierTone(tier: DlcTier): "red" | "orange" | "gray" {
  return tier === "redline" ? "red" : tier === "gate" ? "orange" : "gray";
}

/**
 * Every gate holding at once. A table of eight 95% gates compounds to 66%, which
 * is why the editor shows this next to the thresholds a person just typed.
 */
export function compoundRate(thresholds: number[]): number | null {
  const usable = thresholds.filter((t) => typeof t === "number" && t > 0 && t <= 1);
  if (usable.length === 0) return null;
  return usable.reduce((acc, t) => acc * t, 1);
}

/** One line for the golden versions a release froze, e.g.
 * `regression v3 (15) · holdout 7746f6ab7ec7 (9)`: the version label when the
 * split has one, else its dataset id, plus the item count. */
export function goldenVersionsLabel(golden: GoldenVersions | null | undefined): string {
  const parts = Object.entries(golden ?? {}).map(([split, v]) => {
    if (v == null) return `${split} —`;
    if (typeof v !== "object") return `${split} ${v}`;
    const label = v.version ?? v.dataset_id ?? "—";
    return typeof v.items === "number" ? `${split} ${label} (${v.items})` : `${split} ${label}`;
  });
  return parts.join(" · ") || "—";
}

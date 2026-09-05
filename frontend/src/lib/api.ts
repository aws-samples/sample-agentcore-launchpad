/** Typed client for the Launchpad backend. */

import i18n from "../i18n";
import type { EvaluationRunInfo, InsightTrees } from "./evaluation";
import type { ModelSource } from "./models";

export interface StageInfo {
  name: string;
  status: "pending" | "running" | "succeeded" | "skipped" | "failed";
  detail: string;
  started_at?: string;
  ended_at?: string;
}

export interface DeploymentInfo {
  id: string;
  agent_id: string;
  job_id: string | null;
  status: "running" | "succeeded" | "failed";
  stages: StageInfo[];
  started_at: string | null;
  ended_at: string | null;
}

export interface AgentInfo {
  id: string;
  name: string;
  method: "harness" | "zip_runtime" | "container" | "studio" | "discovered_runtime";
  status: "draft" | "deploying" | "active" | "failed" | "deleted";
  arn: string | null;
  resource_id: string | null;
  version: string | null;
  owner: string;
  error: string | null;
  spec: Record<string, unknown>;
  experiment_capability: {
    eligible: boolean;
    system_prompt: boolean;
    tool_descriptions: boolean;
    reason: string | null;
    reason_code: string | null;
  };
  canary_capability: {
    eligible: boolean;
    reason: string | null;
    reason_code: string | null;
  };
  invoke_capability: {
    eligible: boolean;
    reason: string | null;
    reason_code: string | null;
  };
  created_at: string | null;
  updated_at: string | null;
  deployment?: DeploymentInfo;
  deployments?: DeploymentInfo[];
  revision?: number;
}

/** One row of `GET /api/chat/{agent_id}/sessions`. */
export interface ChatSessionInfo {
  session_id: string;
  actor_id: string;
  turns: number;
  last_at: string | null;
  /** Set once the console explicitly ended the runtime session; `null` while live/idle. */
  ended_at: string | null;
  preview: string;
}

/** `POST /api/chat/{agent_id}/sessions/{session_id}/stop` */
export interface ChatSessionStopResult {
  session_id: string;
  ended: boolean;
  /** AWS no longer knew the session (already ended or idle-expired) — still a success. */
  already_ended: boolean;
  ended_at: string | null;
}

/** GET /api/agents/{id}/versions — the AWS-side version + endpoint set of one
 *  Runtime- or Harness-backed agent (read-only, allow-list projected). */
export interface AgentVersionRow {
  version: string | null;
  status: string | null;
  description: string | null;
  last_updated_at: string | null;
}

export interface AgentEndpointRow {
  name: string | null;
  live_version: string | null;
  target_version: string | null;
  status: string | null;
  description: string | null;
  created_at: string | null;
  last_updated_at: string | null;
  failure_reason: string | null;
}

export interface AgentVersionsInfo {
  kind: "runtime" | "harness";
  resource_id: string;
  versions: AgentVersionRow[];
  endpoints: AgentEndpointRow[];
  /** highest version AWS reports */
  latest_version: string | null;
  /** the version the last Launchpad deploy recorded (Agent.version) */
  ledger_version: string | null;
  /** names among `stable`/`treatment` still present — canary leftovers */
  canary_endpoints: string[];
}

export interface RuntimeDiscoveryCandidate {
  runtime_id: string;
  runtime_arn: string;
  name: string;
  description: string;
  version: string;
  aws_status: string;
  protocol: string;
  artifact_type: "code" | "container" | "harness" | "unknown";
  authorizer_type: "none" | "custom_jwt" | "unknown";
  last_updated_at: string | null;
  managed_agent_id: string | null;
  managed_agent_name: string | null;
  managed_agent_method: AgentInfo["method"] | null;
  importable: boolean;
  reason_code: string | null;
  reason: string | null;
  invoke_capability: {
    eligible: boolean;
    reason: string | null;
    reason_code: string | null;
  };
}

// Managed Harnesses expose no artifact/authorizer detail in ListHarnesses, so the
// scan projects identity + status only; invoke eligibility is decided after import.
export interface HarnessDiscoveryCandidate {
  harness_id: string;
  harness_arn: string;
  name: string;
  description: string;
  version: string;
  aws_status: string;
  last_updated_at: string | null;
  managed_agent_id: string | null;
  managed_agent_name: string | null;
  managed_agent_method: AgentInfo["method"] | null;
  importable: boolean;
  reason_code: string | null;
  reason: string | null;
}

export interface RuntimeDiscoveryResponse {
  region: string;
  runtimes: RuntimeDiscoveryCandidate[];
  harnesses: HarnessDiscoveryCandidate[];
  harness_scan_error: string | null;
}

// One import call carries both kinds; a result row names the kind it came from.
export interface RuntimeImportItem {
  runtime_id?: string;
  harness_id?: string;
  agent_id?: string;
  agent_name?: string;
  reason_code?: string;
  reason?: string;
}

export interface RuntimeImportResult {
  imported: RuntimeImportItem[];
  updated: RuntimeImportItem[];
  already_managed: RuntimeImportItem[];
  failed: RuntimeImportItem[];
}

export interface RuntimeCanaryMetric {
  label: string;
  polarity?: number;  // +1 = higher mean wins, -1 = lower mean wins
  control: { mean: number | null; sampleSize: number | null };
  variants: {
    name: string;
    mean: number | null;
    sampleSize: number | null;
    pValue?: number | null;
    percentChange?: number | null;
    isSignificant?: boolean;
  }[];
}

export interface RuntimeCanaryInfo {
  id: string;
  name: string;
  champion_agent_id: string;
  champion_agent_name: string;
  challenger_agent_id: string;
  challenger_agent_name: string;
  source_experiment_id: string | null;
  status: "running" | "completed" | "rolled_back" | "cleaned";
  stage: string;
  stages: string[];
  running_action: string | null;
  progress: string | null;
  error: string | null;
  created_at: string | null;
  artifacts: {
    agent_meta?: {
      id: string;
      name: string;
      arn: string;
      resource_id: string;
      runtime_name: string;
    };
    edited_spec?: Record<string, unknown>;
    // ``setup`` is persisted as a PARTIAL artifact (the block below) as soon as the
    // gateway + stable endpoint are up, so invoke keeps serving v_current during
    // provisioning. Everything after it only exists once the A/B test is live —
    // treat an absent ab_test_id as "still provisioning", never as a complete setup.
    setup?: {
      gateway_id: string;
      gateway_arn: string;
      gateway_url: string;
      // absent on pre-version-framing canary rows
      v_current?: string;
      stable_endpoint?: string;
      runtime_id?: string;
      test_name?: string;
      ab_test_id?: string;
      ramp_stage?: number;
      weights?: Record<string, number>;
      v_candidate?: string;
      // zip behind v_candidate; cleanup deletes it unless it is still live
      candidate_s3_key?: string;
      treatment_endpoint?: string;
      champion?: {
        target_name: string;
        target_id: string;
        online_eval_id: string;
      };
      challenger?: {
        target_name: string;
        target_id: string;
        online_eval_id: string;
      };
    };
    rounds?: {
      ramp_stage: number;
      weights: Record<string, number>;
      traffic_attempts: {
        sent: number;
        failed: number;
        baseline_n: number;
        dataset_id?: string;
        dataset_name?: string;
        completed_at?: string;
        // diagnostic breakdown of the send (e.g. {"200": 47, "429": 3});
        // absent on attempts recorded before the concurrent send landed
        status_counts?: Record<string, number>;
      }[];
      verdict?: {
        verdict: string;
        avg_delta?: number;
        n?: number;
        significant?: boolean;
        baseline_n?: number;
        reason?: string;
        metrics: RuntimeCanaryMetric[];
      };
    }[];
    complete?: {
      winner: string;
      ab_test_status: string;
      completed_at: string;
      promoted_version?: string;
    };
    rollback?: {
      winner: string;
      restored_version?: string;
      restored_s3_key?: string;
      ab_test_status?: string;
      rolled_back_at?: string;
    };
    cleanup?: { category: string; status: string; detail?: string }[];
  };
}

export interface JobEvent {
  ts: string;
  stage: string;
  level: string;
  msg: string;
}

export interface JobInfo {
  id: string;
  type: string;
  status: "queued" | "running" | "succeeded" | "failed";
  error: string | null;
  events: JobEvent[];
}

export interface ByoMountInput {
  access_point_arn: string;
  mount_path: string;
}

export interface FilesystemInput {
  session_storage: { mount_path: string } | null;
  s3_files: ByoMountInput[];
  efs: ByoMountInput[];
}

export interface VpcNetworkInput {
  subnets: string[];
  security_groups: string[];
}

/**
 * Which agent SDK the "container" method packages — the second-level choice
 * under the console's "Other Agent SDK" entrance. One member today; the field is
 * persisted so a future addition needs no stored-spec migration.
 */
export type AgentSdk = "claude_agent_sdk";

/**
 * A named, platform-owned bundle of local `@tool` functions the Strands zip
 * template inlines into the generated agent. Mirrors the backend `Toolkit`
 * literal (`backend/app/schemas/agent.py`).
 */
export type Toolkit = "hr_assistant";

export interface AgentSpecInput {
  name: string;
  method: string;
  model_id?: string;
  /** Hosting surface of model_id. Omitted ⇒ backend defaults to "bedrock". */
  model_source?: ModelSource;
  /** container method only. Omitted ⇒ backend defaults to "claude_agent_sdk". */
  agent_sdk?: AgentSdk;
  system_prompt: string;
  tool_description_overrides?: Record<string, string>;
  tools?: { type: string; name: string; config?: Record<string, unknown> }[];
  /**
   * Platform-owned local tool sets inlined into the generated agent (zip_runtime
   * only). Not a `tools` entry: a toolkit is source, not an external resource.
   */
  toolkits?: Toolkit[];
  skills?: string[];
  // Managed KB references mounted onto the agent (harness method only).
  knowledge_bases?: { kb_id: string; name: string; description: string }[];
  memory?: { short_term: boolean; long_term: boolean };
  code?: string;
  requirements?: string[];
  env?: Record<string, string>;
  studio_flow?: { nodes: unknown[]; edges: unknown[]; graphMode: boolean };
  filesystem?: FilesystemInput;
  network?: VpcNetworkInput;
}

/** One skill discovered by /api/registry/skills/inspect (zip or git source). */
export interface InspectedSkill {
  index: number;
  name: string;
  description: string;
  version: string;
  files: string[];
  valid: boolean;
  errors: string[];
}

/** Result row from /api/agent-skills/import (attach-without-registering). */
export interface AttachedSkill {
  name: string;
  ok: boolean;
  path?: string;
  description?: string;
  error?: string;
  error_code?: string;
}

export class ApiError extends Error {
  code: string;
  detail: unknown;
  constructor(code: string, message: string, detail: unknown) {
    super(message);
    this.code = code;
    this.detail = detail;
  }
}

export const AUTH_UNAUTHORIZED_EVENT = "launchpad-unauthorized";

/**
 * Console copy for a backend error code, when `apiErrors.<code>` exists.
 *
 * The backend message is operator-facing English written for a log; a code the
 * console has copy for gets that copy instead, so the ~90 catch branches that
 * toast `err.message` show a translated string without each one mapping codes.
 * Pages that map codes themselves (`t(`apiErrors.${code}`, err.message)`) keep
 * working — they resolve the same key.
 */
export function localizedMessage(code: string, fallback: string): string {
  const key = `apiErrors.${code}`;
  return i18n.exists(key) ? i18n.t(key) : fallback;
}

async function parseResponse<T>(path: string, res: Response): Promise<T> {
  // Cloned before the body is consumed, so a parse failure can still show what
  // the wire actually carried — the error is undiagnosable without it.
  const clone = res.clone();
  // `undefined` marks a parse failure — JSON.parse itself can never yield it.
  const body: unknown = await res.json().catch(() => undefined);
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith("/api/auth/")) {
      window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT));
    }
    const env = (body ?? {}) as { code?: string; message?: string; detail?: unknown };
    const code = env.code ?? `http.${res.status}`;
    throw new ApiError(code, localizedMessage(code, env.message ?? res.statusText), env.detail);
  }
  if (body === undefined) {
    // A 200 whose body isn't JSON (backend mid-restart behind the dev proxy,
    // truncated response). Every console endpoint returns a JSON body, so
    // resolving `null` here poisons callers that stored the result as data
    // (e.g. a table's rows) and crashed far from the cause.
    const text = await clone.text().catch(() => "");
    const snippet = text ? JSON.stringify(text.slice(0, 120)) : "<empty body>";
    throw new ApiError(
      "http.invalid_json",
      `invalid JSON response from ${path}: ${snippet}`,
      null,
    );
  }
  return body as T;
}

/** `headers` is narrowed to a plain record so the merge below is exhaustive. */
type RequestInitJson = Omit<RequestInit, "headers"> & { headers?: Record<string, string> };

async function request<T>(path: string, init?: RequestInitJson): Promise<T> {
  const res = await fetch(path, {
    ...init,
    // Merged, not spread over: an init that carries headers (the workspace-
    // targeted job poll) would otherwise drop the JSON content type.
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  return parseResponse<T>(path, res);
}

/** multipart POST — the browser sets the boundary Content-Type itself. */
async function requestForm<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(path, { method: "POST", body: form });
  return parseResponse<T>(path, res);
}

/**
 * GET a console endpoint with the same envelope handling as the typed client,
 * for pages that still fetch by path. A non-2xx or non-JSON answer throws an
 * `ApiError` instead of being folded into an empty result.
 */
export function getJson<T>(path: string): Promise<T> {
  return request<T>(path);
}

/** Console copy for a non-2xx `Response` a page fetched itself (same envelope rules). */
export async function responseMessage(res: Response): Promise<string> {
  try {
    await parseResponse<unknown>(res.url, res);
    return res.statusText;
  } catch (err) {
    return errorMessage(err);
  }
}

/**
 * Console copy for a failed load. `ApiError` messages are already localized;
 * a fetch-level `TypeError` means the request never got an HTTP answer
 * (backend down, network) and gets its own copy instead of "Failed to fetch".
 */
export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof TypeError) return i18n.t("apiErrors.network");
  return err instanceof Error ? err.message : String(err);
}

/* ── governance ────────────────────────────────────────────────────────── */

export type GovernanceGatewayMode = "LOG_ONLY" | "ENFORCE";
export type GovernancePolicyMode = "LOG_ONLY" | "ACTIVE";
export type GovernanceEvidenceRange = "1h" | "6h" | "24h" | "7d";
export type GovernanceAuthorizationModel = "allowlist" | "preserve_traffic" | "custom";
export type GovernanceOperationStatus =
  | "pending"
  | "running"
  | "succeeded"
  | "failed"
  | "partial"
  | "interrupted";

export interface GovernancePolicyEngine {
  id: string;
  arn: string;
  /** Null when the Gateway references an Engine that no longer exists. */
  name: string | null;
  status: string;
  status_reasons: string[];
  updated_at: string | null;
  mode: GovernanceGatewayMode | null;
  /** The referenced Engine was deleted out-of-band; the stale ARN remains. */
  missing: boolean;
}

export interface GovernanceRegistryRecord {
  record_id: string;
  name: string;
  description: string;
  status: string;
  version: string | null;
  url: string;
}

export interface GovernanceAttachability {
  attachable: boolean;
  reason: string | null;
  auth_type: "aws_iam" | "none" | "oauth" | null;
}

export interface GovernanceGatewaySummary {
  id: string;
  arn: string;
  name: string;
  description: string;
  status: string;
  status_reasons: string[];
  protocol_type: string;
  authorizer_type: string;
  url: string | null;
  role_arn: string | null;
  managed: boolean;
  target_count: number;
  targets: {
    id: string;
    name: string;
    status: string;
    description: string;
  }[];
  policy_engine: GovernancePolicyEngine | null;
  shared_gateways: {
    id: string;
    arn: string;
    name: string;
  }[];
  shared_engine: boolean;
  attachability: GovernanceAttachability;
  policy_test_available: boolean;
  registry_record?: GovernanceRegistryRecord | null;
  legacy_record_count?: number;
  updated_at: string | null;
}

export interface GovernanceGatewayTarget {
  id: string;
  name: string;
  status: string;
  status_reasons: string[];
  description: string;
  listing_mode: string | null;
}

export interface GovernanceGatewayAction {
  name: string;
  target_id: string;
  target_name: string;
  description: string;
  input_schema: Record<string, unknown>;
  verified: boolean;
  source: "control_schema" | "live_tools_list" | "manual";
}

export interface GovernanceIamPreflight {
  status: "pass" | "fail" | "unknown";
  missing_actions: string[];
  reason: string | null;
  operator_error?: string | null;
  remediation: Record<string, unknown>;
}

export interface GovernanceGatewayDetail extends GovernanceGatewaySummary {
  authorizer_configuration: Record<string, unknown> | null;
  protocol_configuration: Record<string, unknown> | null;
  targets: GovernanceGatewayTarget[];
  actions: GovernanceGatewayAction[];
  iam_preflight: GovernanceIamPreflight | null;
  external_tools_list_command?: string | null;
}

export interface GovernanceGatewayListResponse {
  gateways: GovernanceGatewaySummary[];
  account_id?: string | null;
  region?: string;
  cached?: boolean;
  cache_age_seconds?: number | null;
}

export interface GovernanceManageResult {
  gateway_id: string;
  managed: boolean;
}

export interface GovernanceRegistryPreview {
  gateway_id: string;
  gateway_name: string;
  gateway_url: string;
  proposed: {
    name: string;
    description: string;
    descriptors: Record<string, unknown>;
  };
  exact_record: GovernanceRegistryRecord | null;
  name_conflict: GovernanceRegistryRecord | null;
  legacy_records: GovernanceRegistryRecord[];
  outcome: "created" | "reused" | "changed" | "conflicted";
  changed: boolean;
}

export interface GovernanceRegistryImportResult {
  outcome: "created" | "reused" | "updated";
  record: GovernanceRegistryRecord;
  submitted: boolean;
  created: number;
  reused: number;
  updated: number;
  skipped: number;
  conflicted: number;
  legacy_records: GovernanceRegistryRecord[];
}

export interface GovernanceValidationFinding {
  type: string;
  message: string;
  severity: string;
  location: string | null;
}

export interface GovernancePolicy {
  id: string;
  arn: string;
  name: string;
  description: string;
  status: string;
  status_reasons: string[];
  enforcement_mode: GovernancePolicyMode;
  statement: string;
  updated_at: string | null;
  candidate_for?: string;
  candidate_id?: string;
  audit_id?: string;
}

export interface GovernancePolicyListResponse {
  gateway: {
    id: string;
    arn: string;
    name: string;
    status: string;
    updated_at: string | null;
    policy_engine_configuration: {
      arn?: string;
      mode?: GovernanceGatewayMode;
    } | null;
  };
  engine: GovernancePolicyEngine | null;
  policies: GovernancePolicy[];
}

export interface GovernanceMutationEnvelope {
  expected_gateway_updated_at?: string | null;
  expected_policy_updated_at?: string | null;
  acknowledged_gateway_ids?: string[];
  confirmation_name?: string | null;
  override_reason?: string | null;
}

export interface GovernanceEngineRequest extends GovernanceMutationEnvelope {
  name?: string | null;
  mode: GovernanceGatewayMode;
  authorization_model: GovernanceAuthorizationModel;
  high_risk_acknowledged: boolean;
}

export interface GovernancePolicyCreateRequest extends GovernanceMutationEnvelope {
  name: string;
  statement: string;
  description?: string | null;
  authorization_model: GovernanceAuthorizationModel;
  high_risk_acknowledged: boolean;
  manual_actions: string[];
}

export interface GovernancePolicyUpdateRequest extends GovernanceMutationEnvelope {
  statement: string;
  description?: string | null;
  manual_actions: string[];
}

export type GovernancePolicyDeleteRequest = GovernanceMutationEnvelope;

export interface GovernancePolicyTransitionRequest extends GovernanceMutationEnvelope {
  evidence_range: GovernanceEvidenceRange;
  audit_id?: string | null;
}

export interface GovernanceGatewayModeRequest extends GovernancePolicyTransitionRequest {
  mode: GovernanceGatewayMode;
}

export interface GovernanceRegistryImportRequest extends GovernanceMutationEnvelope {
  record_name?: string | null;
  apply_update: boolean;
}

export interface GovernanceRetireLegacyRequest extends GovernanceMutationEnvelope {
  record_ids: string[];
}

export interface GovernanceGenerationRequest extends GovernanceMutationEnvelope {
  text: string;
  name: string;
}

export interface GovernanceGeneration {
  id: string;
  status: string;
  status_reasons: string[];
  findings: unknown;
  assets: {
    id: string | null;
    statement: string;
    findings: unknown;
    raw_text_fragment: string | null;
  }[];
}

export type GovernancePolicyTestIdentity = "demo" | "admin";
export type GovernancePolicyTestOutcome = "ALLOW" | "DENY" | "ERROR";

export interface GovernancePolicyTestRequest {
  tool: string;
  arguments: Record<string, unknown>;
  username: GovernancePolicyTestIdentity;
}

export interface GovernancePolicyTestResult {
  principal: string;
  tool: string;
  outcome: GovernancePolicyTestOutcome;
  detail: string;
  policy_id: string | null;
  decision_id: string | null;
  recorded: boolean;
}

export interface GovernanceOperation {
  id: string;
  gateway_id: string;
  gateway_name: string;
  engine_id: string | null;
  policy_id: string | null;
  candidate_policy_id: string | null;
  operation: string;
  operator: string;
  status: GovernanceOperationStatus;
  before: Record<string, unknown>;
  requested: Record<string, unknown>;
  after: Record<string, unknown> | null;
  expected_updated_at: string | null;
  override_reason: string | null;
  error: string | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}

/** `invocation` = a tool call was authorized (or refused) at call time.
 *  `tool_listing` = `PartiallyAuthorizeActions` withheld the tool from the model at
 *  `tools/list` time — nothing was blocked mid-call, the tool was never offered.
 *  Under ENFORCE this is the only DENY that can occur, so it is the common case. */
export type GovernanceDecisionEvaluation = "invocation" | "tool_listing";

export interface GovernancePolicyDecision {
  at: string | null;
  gateway_id: string | null;
  gateway_arn: string | null;
  engine_id: string | null;
  policy_id: string | null;
  determining_policies: string[];
  mismatched_policies: string[];
  /** What a LOG_ONLY candidate policy would have matched — visible even from an
   *  ENFORCE-mode span, and not expressible in the metric channel. */
  log_only_matched_policies: string[];
  /** Always null: the Harness authenticates to the Gateway with an OAuth M2M
   *  client credential, so no span in the trace carries a human principal. */
  principal: string | null;
  /** Human-readable denial reason. Present on DENY only — the span carries it just
   *  for denials, and `tool_listing` rows never have one. */
  reason: string | null;
  action: string | null;
  outcome: "ALLOW" | "DENY" | null;
  engine_mode: GovernanceGatewayMode | null;
  /** Always null: spans carry only the Gateway attachment mode. */
  policy_mode: GovernancePolicyMode | null;
  trace_id: string | null;
  span_id: string | null;
  session_id: string | null;
  evaluation: GovernanceDecisionEvaluation;
  source: "aws";
}

/** `per_call` = one decision per gateway call (`AuthorizeAction`). `per_tool` =
 *  one decision per (call, tool), which is the only granularity AWS publishes for
 *  `PartiallyAuthorizeActions` — so such totals are tool-level, not call-level. */
export type GovernanceEvidenceBasis = "per_call" | "per_tool";

export interface GovernanceEvidenceOperationRow {
  operation: string;
  allow: number;
  deny: number;
  basis: GovernanceEvidenceBasis;
}

export interface GovernanceEvidenceModeRow {
  mode: string;
  allow: number;
  deny: number;
}

export interface GovernanceEvidencePolicyRow {
  policy_id: string;
  allow: number;
  deny: number;
}

export interface GovernanceEvidenceToolRow {
  tool: string;
  allow: number;
  deny: number;
}

export interface GovernanceDecisionResponse {
  range: GovernanceEvidenceRange;
  /** Per-decision rows require Policy spans; empty while the source is metrics only. */
  decisions: GovernancePolicyDecision[];
  /** `decisions.length` — not the evidence total. */
  count: number;
  /** Whether the telemetry channel could be read at all. `true` with
   *  `evidence_count: 0` means a readable channel and a quiet window. */
  available: boolean;
  unavailable_reason: string | null;
  source: "metrics" | "spans" | "metrics+spans";
  evidence_count: number;
  /** Subset of `evidence_count` in LOG_ONLY mode — what the cutover gate needs. */
  log_only_count: number;
  totals: { allow: number; deny: number };
  by_operation: GovernanceEvidenceOperationRow[];
  by_mode: GovernanceEvidenceModeRow[];
  /** Breakdowns, not a decomposition: AWS only publishes the `Policy` dimension
   *  for decisions that had a determining policy, so these need not sum to the
   *  total. */
  by_policy: GovernanceEvidencePolicyRow[];
  by_tool: GovernanceEvidenceToolRow[];
  mismatch: { determining: number; no_determining: number; errors: number };
  /** Some metric streams or span rows were dropped by a per-request cap. */
  truncated: boolean;
  /** Set when the span channel could not be read; the aggregates are still valid. */
  spans_unavailable_reason: string | null;
  /** Live configuration state for the selected Gateway's TRACES delivery. */
  span_channel_status: "ready" | "missing" | "unknown";
  /** Missing component or AWS error code explaining a non-ready channel. */
  span_channel_reason: string | null;
  /** A policy filter was applied but some operations publish no `Policy`
   *  dimension, so their decisions are unattributable and excluded. */
  policy_filter_partial: boolean;
  cache: ObsCache;
}

export interface GovernancePolicyChange {
  id: string;
  gateway_id: string;
  gateway_name: string;
  engine_id: string | null;
  policy_id: string | null;
  candidate_policy_id: string | null;
  operation: string;
  operator: string;
  status: GovernanceOperationStatus;
  before: Record<string, unknown>;
  requested: Record<string, unknown>;
  after: Record<string, unknown> | null;
  expected_updated_at: string | null;
  override_reason: string | null;
  error: string | null;
  created_at: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface GovernanceAuditResponse {
  changes: GovernancePolicyChange[];
}

export type GovernanceRatePeriod = "second" | "minute";
export type GovernanceRateMetric = "requests" | "tokens" | "connections";

export interface GovernanceRateConfig {
  rate: number;
  period: GovernanceRatePeriod;
}

/** One `LimitEntry`: dimension values (in the parent's key set) + per-metric rate. */
export interface GovernanceRateLimitEntry {
  dimensions: Record<string, string>;
  requests?: GovernanceRateConfig[];
  tokens?: GovernanceRateConfig[];
  connections?: GovernanceRateConfig[];
}

export interface GovernanceRateLimit {
  id: string;
  gateway_id: string | null;
  description: string;
  /** Immutable after creation; `*` is only allowed in trailing positions. */
  dimension_keys: string[];
  entries: GovernanceRateLimitEntry[];
  status: string; // CREATING | ACTIVE | UPDATING | DELETING
  created_at: string | null;
  updated_at: string | null;
}

export interface GovernanceRateLimitListResponse {
  rate_limits: GovernanceRateLimit[];
}

export interface GovernanceRateLimitCreateRequest {
  dimension_keys: string[];
  entries: GovernanceRateLimitEntry[];
  description?: string | null;
}

export interface GovernanceRateLimitUpdateRequest {
  entries: GovernanceRateLimitEntry[];
  description?: string | null;
}

export interface GovernanceRateLimitDeleteResult {
  deleted: boolean;
  id: string;
  status: string;
}

export interface GovernanceToolInfo {
  name: string;
  source: "gateway" | "builtin";
  target?: string;
  description: string;
  inputSchema: Record<string, unknown>;
  auth: string;
}

export interface GovernanceToolCatalog {
  tools: GovernanceToolInfo[];
  gateway_url: string | null;
}

export interface CodeInterpreterDemoResult {
  stdout: string;
  session_id: string;
  latency_ms: number;
}

export interface BrowserDemoResult {
  url: string;
  title: string;
  session_id: string;
  latency_ms: number;
  live_view_url: string;
  live_view_expires_in: number;
  viewport: {
    width: number;
    height: number;
  };
  browser_identifier: string;
  web_bot_auth: boolean;
  profile_identifier: string | null;
  save_profile: boolean;
}

export interface BrowserDemoBrowserOption {
  identifier: string;
  name: string;
  description: string;
  status: string;
  web_bot_auth: boolean;
}

export interface BrowserDemoProfileOption {
  identifier: string;
  name: string;
  description: string;
  status: string;
  last_saved_at: string | null;
  last_saved_browser_identifier: string | null;
}

export interface BrowserDemoOptions {
  browsers: BrowserDemoBrowserOption[];
  profiles: BrowserDemoProfileOption[];
}

export interface BrowserDemoRequest {
  url: string;
  web_bot_auth: boolean;
  browser_identifier: string | null;
  profile_identifier: string | null;
  save_profile: boolean;
}

/* ── memory (AgentCore Memory console — read-only) ─────────────────────── */

export interface MemoryStrategy {
  strategy_id: string | null;
  name: string | null;
  description: string | null;
  /** SEMANTIC | USER_PREFERENCE | SUMMARIZATION | CUSTOM */
  type: string | null;
  status: string | null;
  namespaces: string[];
  namespace_templates: string[];
  created_at: string | null;
  updated_at: string | null;
}

export interface MemoryResource {
  id: string;
  arn: string | null;
  name: string | null;
  description: string | null;
  status: string | null;
  failure_reason: string | null;
  event_expiry_days: number | null;
  encryption_key_arn: string | null;
  execution_role_arn: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface MemorySibling {
  id: string | null;
  arn: string | null;
  status: string | null;
  created_at: string | null;
  updated_at: string | null;
  /** true for the singleton this console manages */
  is_platform: boolean;
}

export interface MemoryOverview {
  /** false before `make bootstrap` has provisioned the memory resource */
  configured: boolean;
  memory: MemoryResource | null;
  strategies: MemoryStrategy[];
  actor_count: number;
  /** the count above is one page only; true means there are more actors */
  actor_count_truncated: boolean;
  other_memories: MemorySibling[];
}

/** One row of the memory *resource management* view (`/api/memory/resources`). */
export interface MemoryResourceRow {
  id: string | null;
  arn: string | null;
  /** derived from the id (`{name}-{suffix}`) — ListMemories carries no name */
  name: string | null;
  status: string | null;
  created_at: string | null;
  updated_at: string | null;
  /** the workspace's bootstrap memory — delete-protected default */
  is_default: boolean;
  /** live agents whose spec pins this memory (default users excluded) */
  agents: { id: string; name: string }[];
}

export interface MemoryResourceList {
  items: MemoryResourceRow[];
  default_id: string | null;
}

/** One long-term strategy as `GET/PUT /api/memory/resources/{id}` project it. */
export interface MemoryResourceStrategy {
  strategy_id: string | null;
  name: string | null;
  type: string | null;
  status: string | null;
  namespaces: string[];
}

/** Flexible namespace variable key as defined on the resource. */
export interface MemoryResourceNamespaceKey {
  key: string | null;
  allowed_values: string[] | null;
  regex_pattern: string | null;
}

/** Full detail of one memory resource (`GET /api/memory/resources/{id}`; also the
 *  reply of `POST /api/memory/resources` and `PUT /api/memory/resources/{id}`).
 *  No `agents` here — that annotation exists on the list rows only. */
export interface MemoryResourceDetail {
  id: string | null;
  arn: string | null;
  name: string | null;
  description: string | null;
  status: string | null;
  failure_reason: string | null;
  event_expiry_days: number | null;
  execution_role_arn: string | null;
  created_at: string | null;
  updated_at: string | null;
  is_default: boolean;
  strategies: MemoryResourceStrategy[];
  namespace_keys: MemoryResourceNamespaceKey[];
}

/** `PUT /api/memory/resources/{id}` — both optional, at least one required.
 *  Only these two fields are editable from the console: strategies, namespace
 *  keys, execution role, indexed keys and stream delivery are never sent. */
export interface MemoryResourceUpdateInput {
  /** 1–4096 chars — UpdateMemory can replace a description but never clear it */
  description?: string;
  /** 7–365 days */
  event_expiry_days?: number;
}

export interface MemoryResourceCreateInput {
  /** CreateMemory name constraint: `[a-zA-Z][a-zA-Z0-9_]{0,47}` */
  name: string;
  description?: string;
  event_expiry_days?: number;
  /** subset of "semantic" | "user_preference" | "summarization" | "episodic" */
  strategies?: string[];
  /** flexible namespace variables — up to 5 keys per memory resource */
  namespace_keys?: MemoryNamespaceKeyInput[];
}

/** One custom namespace variable key (CreateMemory `namespaceKeys` entry).
 *  Values are supplied at runtime via CreateEvent's
 *  `extractionConfig.namespaceVariables`; both rules apply when both are set. */
export interface MemoryNamespaceKeyInput {
  /** lowercase alphanumeric, starts with a letter, max 32 chars */
  key: string;
  /** up to 10 permitted values: `[a-z0-9][a-z0-9_-]*`, max 64 chars each */
  allowed_values?: string[];
  /** regex the runtime value must match, max 64 chars */
  regex_pattern?: string;
}

/** AgentCore keys memory on actorId alone, so the platform folds the agent in:
 *  `<agent_id>__<human>`. These fields are that id decoded. */
export interface MemoryActor {
  actor_id: string;
  agent_id: string | null;
  agent_name: string | null;
  human_actor: string;
  scoped: boolean;
}

export interface MemorySessionLedger {
  agent_id: string;
  agent_name: string | null;
  human_actor: string;
  turns: number;
  message_count: number;
}

export interface MemorySessionRow {
  session_id: string;
  actor_id: string;
  created_at: string | null;
  /** null for sessions the console never wrote (eval runs, /v1 callers) */
  ledger: MemorySessionLedger | null;
}

export interface MemoryEventPayload {
  kind: "conversational" | "blob";
  role: string | null;
  text: string | null;
  /** Harness message-envelope part kinds (text / toolUse / toolResult …); empty
   *  for plain-text turns. Lets a tool-only turn render as itself. */
  parts: string[];
  blob_bytes: number | null;
}

export interface MemoryEvent {
  event_id: string | null;
  at: string | null;
  branch: { name: string | null; root_event_id: string | null } | null;
  metadata: Record<string, unknown>;
  payload: MemoryEventPayload[];
}

export interface MemoryNamespace {
  strategy_id: string | null;
  strategy_name: string | null;
  strategy_type: string | null;
  template: string;
  namespace: string;
  /** false when a placeholder other than {actorId} remains unresolved */
  resolvable: boolean;
}

export interface MemoryRecord {
  record_id: string | null;
  /** Human-readable line: prose for SEMANTIC records, the extracted display
   *  field for strategies that store a structured object. */
  text: string;
  /** Parsed payload when the strategy stores JSON (USER_PREFERENCE,
   *  SUMMARIZATION), else null. */
  structured: Record<string, unknown> | null;
  /** The payload exactly as stored — never lost to the display transform. */
  raw_text: string;
  strategy_id: string | null;
  namespaces: string[];
  created_at: string | null;
  /** populated only by semantic retrieval */
  score: number | null;
  metadata: Record<string, unknown>;
}

export interface MemoryPage<T> {
  items: T[];
  next_token: string | null;
}

export interface MemoryRecordPage extends MemoryPage<MemoryRecord> {
  namespace: string;
  query?: string;
}

export interface MemorySearchInput {
  query: string;
  actor_id?: string | null;
  strategy_id?: string | null;
  namespace?: string | null;
  top_k?: number;
}

/** Drops empty/nullish params so the backend never sees `?x=` (the preview
 *  memory API rejects empty strings inside its filter shape). */
function memoryQuery(params: Record<string, string | number | boolean | null | undefined>) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === "") continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

/* ── observability ─────────────────────────────────────────────────────── */

export interface ObsCache {
  hit: boolean;
  age_seconds: number;
}

export interface ObsTokens {
  input: number;
  output: number;
  total: number;
  cache_read?: number;
  cache_write?: number;
}

export interface ObsPricesMeta {
  updated_at?: string;
  source?: string;
  source_models?: number;
  updated?: string[];
  added?: string[];
}

export interface ObsDashboard {
  range: string;
  prices_meta?: ObsPricesMeta | null;
  tiles: {
    traces: { total: number; ok: number; error: number };
    sessions: { total: number; agents: number };
    error_rate: number;
    latency: { p50_ms: number; p95_ms: number };
    tokens: { input: number; output: number; total: number; est_cost_usd: number | null };
  };
  series: { bucket: string; traces: number; errors: number; p50_ms: number; p95_ms: number }[];
  tokens_by_model: {
    model: string;
    input: number;
    output: number;
    total: number;
    est_cost_usd: number | null;
  }[];
  top_tools: { tool: string; calls: number; errors: number; success_rate: number | null }[];
  cache: ObsCache;
}

export interface ObsTraceRow {
  trace_id: string;
  time: string | null;
  root_operation: string;
  service: string | null;
  agent: string;
  session_id: string | null;
  duration_ms: number;
  span_count: number;
  llm_count: number;
  error_count: number;
  status: "ok" | "error";
  model: string | null;
  multi_model: boolean;
  tokens: ObsTokens;
  est_cost_usd: number | null;
}

export interface ObsTraces {
  range: string;
  traces: ObsTraceRow[];
  count: number;
  limit: number;
  cache: ObsCache;
}

export interface ObsSpan {
  span_id: string | null;
  parent_span_id: string | null;
  name: string;
  category: "llm" | "tool" | "memory" | "gateway" | "http" | "agent" | "other";
  kind: string | null;
  status: string;
  start_offset_ms: number;
  duration_ms: number;
  offset_pct: number;
  width_pct: number;
  model: string | null;
  finish_reason: string | string[] | null;
  tool_name: string | null;
  tokens: { input: number; output: number; cache_read: number; cache_write: number } | null;
  est_cost_usd: number | null;
}

export interface ObsSpanNode extends ObsSpan {
  depth: number;
  children: ObsSpanNode[];
}

export interface ObsMessageBlock {
  type: "text" | "tool_use" | "tool_result" | "other";
  text?: string;
  name?: string | null;
  input?: string;
  status?: string | null;
}

export interface ObsSpanMessage {
  role: string | null;
  finish_reason?: string;
  blocks: ObsMessageBlock[];
}

export interface ObsSpanMessages {
  input?: ObsSpanMessage[];
  output?: ObsSpanMessage[];
}

export interface ObsTraceDetail {
  trace_id: string;
  range: string;
  meta: {
    root_operation: string | null;
    service: string | null;
    agent: string;
    session_id: string | null;
    start: string | null;
    duration_ms: number;
    span_count: number;
    llm_count: number;
    status: "ok" | "error";
    tokens: ObsTokens;
    est_cost_usd: number | null;
  };
  tree: ObsSpanNode[];
  spans: (ObsSpan & {
    attributes: Record<string, unknown>;
    messages?: ObsSpanMessages | null;
  })[];
  cache: ObsCache;
}

export interface ObsSessionRow {
  session_id: string;
  service: string | null;
  agent: string;
  traces: number;
  llm_calls: number;
  errors: number;
  tokens: ObsTokens;
  est_cost_usd: number | null;
  first: string | null;
  last: string | null;
  platform: boolean;
}

export interface ObsSessions {
  range: string;
  sessions: ObsSessionRow[];
  count: number;
  limit: number;
  cache: ObsCache;
}

export interface ObsTranscriptTurn {
  role: string;
  text: string;
  at: string;
}

export interface ObsTranscript {
  available: boolean;
  reason?: string;
  detail?: string;
  actor_id?: string;
  agent_id?: string;
  agent_name?: string | null;
  // "experiment"/"external": sessions with no platform ledger row, resolved
  // straight from memory (A/B gateway traffic, /v1 callers, direct invokes)
  source?: "chat" | "eval" | "experiment" | "external";
  origin?: "memory" | "logs";
  run_id?: string | null;
  experiment_id?: string | null;
  experiment_name?: string | null;
  turns?: ObsTranscriptTurn[];
  long_term_records?: number | null;
}

export interface ObsSessionDetail {
  session_id: string;
  range: string;
  summary: {
    agent: string | null;
    traces: number;
    llm_calls: number;
    errors: number;
    tokens: ObsTokens;
    est_cost_usd: number | null;
    first: string | null;
    last: string | null;
  };
  traces: ObsTraceRow[];
  transcript: ObsTranscript;
  /** online evaluation results for this session (additive; absent on older backends) */
  online_scores?: OnlineSessionScores;
  cache: ObsCache;
}

/** One judged result record (online evaluation) for a session. */
export interface OnlineSessionScoreRecord {
  time: string | null;
  evaluator_id: string;
  level: string | null;
  score: number | null;
  label: string | null;
  explanation: string | null;
  trace_id: string | null;
}

export interface OnlineSessionScoreConfig {
  config_id: string;
  config_name: string | null;
  owner: OnlineEvalOwner;
  agent: { id: string; name: string } | null;
  records: OnlineSessionScoreRecord[];
}

export interface OnlineSessionScores {
  configs: OnlineSessionScoreConfig[];
  total: number;
  /** the results query failed — traces/transcript are still valid */
  unavailable: boolean;
  /** the workspace has at least one agent-owned online config */
  configs_exist: boolean;
}

/** ONLINE QUALITY · 24h tile — polarity-normalised mean over agent-owned configs. */
export interface OnlineQuality {
  range: string;
  mean: number | null;
  scores: number;
  sessions: number;
  agents: number;
  configs: number;
  evaluators: { evaluator_id: string; mean: number; count: number; polarity: number }[];
  cached: boolean;
}

function obsQuery(range: string, force: boolean): string {
  return `range=${encodeURIComponent(range)}${force ? "&force=true" : ""}`;
}

function governanceGatewayPath(gatewayId: string): string {
  return `/api/governance/gateways/${encodeURIComponent(gatewayId)}`;
}

export interface OverviewInfo {
  registry_assets: { agents: number; tools: number; skills: number; total: number };
  active_sessions: number;
  eval_pass_rate: number | null;
  eval_runs: number;
  services: Record<string, boolean>;
  service_detail: Record<string, string>;
}

export type ConsoleRole = "admin" | "member";

/** Member-grantable agent-management capabilities (default granted). */
export type AgentPermission =
  | "agents.deploy"
  | "agents.import"
  | "agents.delete"
  | "agents.convert"
  | "eval.run";

export const AGENT_PERMISSIONS: AgentPermission[] = [
  "agents.deploy",
  "agents.import",
  "agents.delete",
  "agents.convert",
  "eval.run",
];

export interface AuthStatus {
  auth_required: boolean;
  authenticated: boolean;
  registration_enabled: boolean;
  /** new registrations wait in `pending` until an admin approves them */
  registration_requires_approval: boolean;
  username: string | null;
  role: ConsoleRole | null;
  email: string | null;
  /** account validity (registered users); null = built-in admin / never expires */
  account_expires_at: string | null;
  /** granted agent-management permissions ([] until authenticated) */
  permissions: AgentPermission[];
}

export interface AuthLoginResult extends AuthStatus {
  ok: boolean;
  /** session-cookie expiry (epoch seconds), clamped to the account validity */
  expires_at: number | null;
}

export interface RegisterResult {
  ok: boolean;
  username: string;
  email: string;
  status: "pending" | "active";
  requires_approval: boolean;
  /** null while pending — the validity window starts at approval */
  expires_at: string | null;
  valid_days: number;
}

/* ── online evaluation (continuous, sampled scoring of live sessions) ───── */

export type OnlineEvalOwner = "agent" | "experiment" | "external";

/** `scores` = evaluators judge each sampled session; `insights` = sampled
 *  sessions are clustered into recurring failure / intent / summary reports.
 *  Derived server-side (`insights` non-empty → insights); immutable after create. */
export type OnlineEvalMode = "scores" | "insights";
export type OnlineEvalFrequency = "DAILY" | "WEEKLY" | "MONTHLY";
export type OnlineEvalRange = "1h" | "6h" | "24h" | "7d";

export type OnlineEvalFilterOperator =
  | "Equals"
  | "NotEquals"
  | "GreaterThan"
  | "LessThan"
  | "GreaterThanOrEqual"
  | "LessThanOrEqual"
  | "Contains"
  | "NotContains";

/** Exactly one of the value branches is set (AWS `rule.filters[].value`). */
export interface OnlineEvalFilter {
  key: string;
  operator: OnlineEvalFilterOperator;
  value: { stringValue?: string; doubleValue?: number; booleanValue?: boolean };
}

/**
 * One shape for list rows and Get details — `detailed` tells which: list
 * summaries of experiment-owned configs carry no evaluators/rule (AWS lists
 * lack them and the page does not enrich those rows).
 */
export interface OnlineEvalConfigRow {
  config_id: string;
  arn: string | null;
  name: string | null;
  description: string;
  owner: OnlineEvalOwner;
  status: string | null;
  execution_status: string | null;
  failure_reason: string | null;
  agent_id: string | null;
  agent_name: string | null;
  matched_agent: { id: string; name: string } | null;
  detailed: boolean;
  mode: OnlineEvalMode;
  evaluators: string[];
  sampling_percentage: number | null;
  session_timeout_minutes: number | null;
  filter_count: number;
  filters: OnlineEvalFilter[];
  data_source: { log_groups: string[]; service_name: string | null };
  insights: string[];
  clustering_frequencies: string[];
  execution_role_arn: string | null;
  results_log_group: string;
  duplicate_enabled: boolean;
  created_at: string | null;
  updated_at: string | null;
}

/** `POST /api/eval/online`. Exactly one kind per mode: `evaluators` for scores,
 *  `insights` (+ `clustering_frequencies`) for insights — mixing → 422
 *  `online_eval.mode_conflict`. Omitting `sampling_percentage` takes the AWS
 *  default (10 for scores, 100 for insights). */
export interface OnlineEvalConfigCreate {
  agent_id: string;
  mode?: OnlineEvalMode;
  evaluators?: string[];
  insights?: string[];
  clustering_frequencies?: OnlineEvalFrequency[];
  sampling_percentage?: number;
  session_timeout_minutes: number;
  filters: OnlineEvalFilter[];
  description?: string | null;
  enable_on_create: boolean;
}

/** `PATCH /api/eval/online/{id}` — only changed fields travel; the rule is
 *  merged server-side. `insights` / `clustering_frequencies` are complete
 *  lists (insights mode only), `evaluators` is scores mode only. */
export interface OnlineEvalConfigPatch {
  description?: string;
  evaluators?: string[];
  insights?: string[];
  clustering_frequencies?: OnlineEvalFrequency[];
  sampling_percentage?: number;
  session_timeout_minutes?: number;
  filters?: OnlineEvalFilter[];
}

export interface OnlineEvalResultsEvaluator {
  evaluator_id: string;
  level: string | null;
  mean: number | null;
  count: number;
  sessions: number;
  labels: Record<string, number>;
}

export interface OnlineEvalResultsPoint {
  bucket: string;
  mean: number | null;
  count: number;
}

export interface OnlineEvalResultsRecord {
  time: string | null;
  session_id: string | null;
  trace_id: string | null;
  evaluator_id: string | null;
  level: string | null;
  score: number | null;
  label: string | null;
  explanation: string | null;
  error: string | null;
}

export interface OnlineEvalResults {
  range: string;
  log_group: string;
  evaluators: OnlineEvalResultsEvaluator[];
  series: Record<string, OnlineEvalResultsPoint[]>;
  recent: OnlineEvalResultsRecord[];
  errors: { count: number; first_message: string | null };
}

/** Session counters of one report (a batch evaluation). `total` is null until
 *  the batch exists. */
export interface OnlineEvalReportSessions {
  completed: number;
  failed: number;
  in_progress: number;
  total: number | null;
}

/**
 * One insights report of an online config. `aws_scheduled` reports are created
 * by AWS on the clustering cadence; `console` reports are on-demand runs
 * (also visible on the Runs page as `online:<configId>` insights runs).
 * `status` is the AWS batch status, or the uppercased run status for a console
 * run that has no batch yet (e.g. "QUEUED"); `batch_id` is null in that case.
 */
export interface OnlineEvalReportRow {
  batch_id: string | null;
  name: string | null;
  status: string | null;
  run_status: string | null;
  created_at: string | null;
  updated_at: string | null;
  insights: string[];
  sessions: OnlineEvalReportSessions;
  origin: "console" | "aws_scheduled";
  run_id: string | null;
  error: string | null;
}

export interface OnlineEvalReports {
  config_id: string;
  mode: OnlineEvalMode;
  /** newest first */
  reports: OnlineEvalReportRow[];
  /** insights batches that could not be attributed to any config (best-effort;
   *  absent when attribution is exact) */
  unattributed?: OnlineEvalReportRow[];
  /** ListBatchEvaluations failed — only console (ledger) rows are present */
  aws_unavailable?: boolean;
}

export interface OnlineEvalReportDetail {
  batch_id: string;
  name: string | null;
  status: string | null;
  created_at: string | null;
  updated_at: string | null;
  time_range: { startTime?: string | null; endTime?: string | null } | null;
  sessions: OnlineEvalReportSessions;
  /** same trees the Runs page renders for a batch insights run */
  insights: InsightTrees;
  error_details: string[];
}

export interface OnlineEvalRunReportAck {
  run_id: string;
  status: string;
  queue_position: number | null;
  range: OnlineEvalRange;
  config_id: string;
}

/* ── workspaces (the environment a request targets) ────────────────────── */

export type WorkspaceBootstrapStatus = "registered" | "bootstrapping" | "ready" | "failed";

export interface Workspace {
  id: string;
  name: string;
  account_id: string;
  region: string;
  /** reached through an assumed role in another account */
  cross_account: boolean;
  /** the assumed role, for admins only — absent from a member's list */
  role_arn?: string | null;
  bootstrap_status: WorkspaceBootstrapStatus;
  /** the hub's own environment: cannot be deleted or bootstrapped from here */
  is_default: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface WorkspaceListResult {
  workspaces: Workspace[];
  /** true when the caller is an admin, i.e. the list is every workspace */
  all_workspaces: boolean;
}

export interface WorkspaceGrantUser {
  id: string;
  username: string;
  email: string;
  role: ConsoleRole;
  /** derived account state, as on the Users page */
  status: UserState;
  /** whether this account holds the workspace being listed */
  granted: boolean;
}

export type WorkspaceGrantFilter = "all" | "granted" | "ungranted";

/**
 * One page of the member accounts a workspace can be granted to.
 *
 * Admins are deliberately absent: they reach every workspace by role, so a row
 * offering to revoke would be a lie. `total` follows the search and filter (it
 * drives the pager); `granted_total` is a property of the workspace and does
 * not, so it stays put while the operator types.
 */
export interface WorkspaceGrants {
  workspace_id: string;
  users: WorkspaceGrantUser[];
  total: number;
  granted_total: number;
  limit: number;
  offset: number;
}

/** What a batch grant/revoke actually changed. */
export interface WorkspaceGrantsResult {
  workspace_id: string;
  added: number;
  removed: number;
  granted_total: number;
}

export interface WorkspaceBootstrapAck {
  job_id: string;
  workspace_id: string;
  bootstrap_status: WorkspaceBootstrapStatus;
  stages: StageInfo[];
}

/** The bootstrap job's per-stage records live on its payload, not on a
 * Deployment row (there is no agent involved). */
export interface WorkspaceBootstrapJob extends JobInfo {
  payload?: { workspace_id?: string; stages?: StageInfo[] } | null;
}

/**
 * The verdict of an access probe on a cross-account pair.
 *
 * `ok: false` arrives as a 200 — a refused AssumeRole is the answer to the
 * question, not a failure of the request — so the diagnostic renders inline
 * instead of as a thrown error.
 */
export interface WorkspacePreflightResult {
  ok: boolean;
  /** the account the assumed role actually reached, when ok */
  caller_account: string | null;
  /** operator-actionable text when the role could not be assumed */
  diagnostic: string | null;
}

/**
 * What a purge removed, or — with `dry_run` — would remove.
 *
 * `rows` is keyed by ledger table; `resource_keys` names the resource kinds a
 * failed bootstrap had already provisioned in AWS, which a purge does NOT
 * delete. Values are never disclosed, only which kinds exist.
 */
export interface WorkspacePurgeResult {
  purged: boolean;
  dry_run: boolean;
  workspace_id: string;
  rows: Record<string, number>;
  resource_keys: string[];
}

/* ── console accounts (admin user management) ──────────────────────────── */

export type UserState = "pending" | "active" | "expired" | "disabled";
export type UserStatusFilter = "all" | UserState;

export interface ConsoleUser {
  id: string;
  username: string;
  email: string;
  role: ConsoleRole;
  status: "pending" | "active" | "disabled";
  state: UserState;
  expires_at: string | null;
  days_remaining: number | null;
  created_at: string;
  last_login_at: string | null;
  login_count: number;
  created_by: string;
  /** effective agent-management permission map (admins: all true) */
  permissions: Record<AgentPermission, boolean>;
  /** granted workspace ids; empty for an admin, who reaches every workspace */
  workspaces: string[];
  /** only present on a password reset the platform generated */
  generated_password?: string;
}

export interface UserListResult {
  items: ConsoleUser[];
  total: number;
  limit: number;
  offset: number;
}

export interface UserStats {
  total: number;
  pending: number;
  active: number;
  expired: number;
  disabled: number;
  expiring_soon: number;
  registered_last_7d: number;
  active_last_7d: number;
  registrations: { date: string; count: number }[];
  top_domains: { domain: string; count: number }[];
  valid_days: number;
}

export interface UserPatchBody {
  /** "active" on a pending account approves it and starts its validity window */
  status?: "pending" | "active" | "disabled";
  role?: ConsoleRole;
  extend_days?: number;
  /** ISO timestamp, or null for "never expires" */
  expires_at?: string | null;
  /** null asks the backend to generate one and return it once */
  password?: string | null;
  /** partial map; unsent keys stay granted, null resets to all-granted */
  permissions?: Partial<Record<AgentPermission, boolean>> | null;
  /** full replacement of the account's workspace grants */
  workspaces?: string[];
}

/* ── skill lab (skill evaluation & optimization) ────────────────────────── */

export interface SkillLabStatus {
  provisioned: boolean;
  /** workspace resource keys the exec worker still needs */
  missing: string[];
  venv_ready: boolean;
  /** platform defaults a job runs with when its params omit the models */
  default_target_model: string;
  default_judge_model: string;
  /** blank-model default for the codex_exec backend (a codex catalog slug) */
  default_codex_target_model: string;
  /** exec backends baked into the worker image */
  target_backends: SkillLabTargetBackend[];
  judge_modes: SkillLabJudgeMode[];
  /** host-side sandbox launcher present. The SHARED agentic-judge prerequisite
   *  only: the judge also shells out to the CLI its route resolves to, so this
   *  being true does not by itself mean artifact tasks can be judged. */
  agentic_judge_ready: boolean;
  /** host codex CLI present — an openai-family judge model routes the
   *  agentic judge to codex, so auto/agentic needs it for artifact tasks */
  judge_codex_ready: boolean;
  /** host claude CLI present — every non-openai judge model routes there.
   *  Its absence is what made prod report "ready" while every artifact task
   *  died with FileNotFoundError. */
  judge_claude_ready: boolean;
  /** readiness for the CONFIGURED DEFAULT judge model's route, for callers that
   *  do not reimplement the routing rule. The wizards use the two probes above,
   *  since they know which model is selected. */
  judge_cli_ready: boolean;
}

export type SkillLabTargetBackend = "claude_code_exec" | "codex_exec";
/** auto = per task (chat for text-only, agentic when artifacts need inspection) */
export type SkillLabJudgeMode = "auto" | "chat" | "agentic";

export type SkillLabTasksetMode = "single" | "split";

export interface SkillLabTasksetInfo {
  id: string;
  name: string;
  description: string;
  mode: SkillLabTasksetMode;
  /** built-in demo sample: read-only (update/delete/expansion refuse with 409) */
  sample: boolean;
  /** {tasks: n} in single mode, {train, val, test?} in split mode */
  counts: Record<string, number>;
  created_at: string | null;
  updated_at: string | null;
}

/**
 * One skilleval task. `id`/`question`/`rubric` are required by the vendored
 * loader; every other key (`files`, `judge_mode`, `artifact_checks`, anything
 * the CLI grows later) is carried through untouched on edit.
 */
export interface SkillLabTask {
  id: string;
  question: string;
  rubric: string;
  task_type?: string;
  /**
   * taskgen only, and only until import: names of the attached documents this
   * task needs. The backend replaces it with `files` asset descriptors when the
   * reviewed output is saved, so a stored task never carries it.
   */
  attachments?: string[];
  [key: string]: unknown;
}

export interface SkillLabAssetDescriptor {
  asset?: `sha256:${string}`;
  staged_asset?: string;
  name: string;
  media_type: string;
  size: number;
}

export interface SkillLabTaskAssetUploadResponse {
  assets: SkillLabAssetDescriptor[];
}

export interface SkillLabTasksetDetail {
  info: SkillLabTasksetInfo;
  tasks_by_split: Record<string, SkillLabTask[]>;
  /** true when a split was capped at the preview size (ask for full) */
  truncated: boolean;
}

export interface SkillLabTasksetBody {
  name: string;
  description?: string;
  mode: SkillLabTasksetMode;
  tasks_by_split: Record<string, SkillLabTask[]>;
}

/** 422 `skill_lab.taskset_invalid` detail: the validator's own message per split. */
export interface SkillLabTasksetIssue {
  split: string;
  message: string;
}

export type SkillLabJobStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"
  | "interrupted";

/** Where the evaluated skill came from; `record_id` only on registry sources. */
export interface SkillLabSkillSource {
  kind: "registry" | "upload";
  /** absent on multi-skill taskgen sources (which carry `names` instead) */
  name?: string;
  record_id?: string;
  version?: string;
  /** taskgen multi-skill source only */
  record_ids?: string[];
  names?: string[];
}

export type SkillLabGateMetric = "hard" | "soft" | "mixed";

export interface SkillLabJobParams {
  /** exec backend running the tasks (default claude_code_exec) */
  target_backend?: SkillLabTargetBackend;
  target_model: string;
  judge_model: string;
  /** eval/train: how verdicts are produced (default auto) */
  judge_mode?: SkillLabJudgeMode;
  /** taskgen only: the generation agent's model (no judge/target split) */
  model?: string;
  /** taskgen only: how many tasks to author (1-30) */
  count?: number;
  /** taskgen only: free-text steering folded into the generation prompt */
  guidance?: string;
  /**
   * taskgen only: names of the documents the job was submitted with, recorded at
   * submission so the panel can show them for a running or failed job too
   * (`gen_summary` only exists once generation succeeded).
   */
  attachment_names?: string[];
  /** taskgen only: present once the operator saved the reviewed output */
  imported_taskset_id?: string;
  /** taskgen expansion only: true once applied to the target task set */
  expanded?: boolean;
  workers: number;
  timeout: number;
  limit: number;
  /** train jobs only */
  epochs?: number;
  /** train jobs only: max edits accepted per step ("learning rate") */
  learning_rate?: number;
  /** train jobs only: which score the held-out gate compares */
  gate_metric?: SkillLabGateMetric;
  /** train jobs only: extra skill files (relative paths) that co-evolve with
   *  SKILL.md as one bundle; empty/omitted = SKILL.md-only training */
  trainable_files?: string[];
}

export interface SkillLabJobInfo {
  id: string;
  type: "eval" | "train" | "taskgen";
  status: SkillLabJobStatus;
  /** 0 unless the job is still waiting behind another one in the queue. */
  queue_position: number;
  /** Live phrase derived from the CLI log tail ("rollout 3 tasks"); never stored. */
  progress: string;
  skill_source: SkillLabSkillSource | null;
  taskset_id: string;
  taskset_name: string;
  /** "" for single-mode task sets. */
  split: string;
  params: SkillLabJobParams;
  error: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

/**
 * Body for `POST /api/skill-lab/jobs`; params fall back to platform defaults.
 * `split` is an evaluation-only choice — training always uses the whole task
 * set (single-mode sets are auto-split 4:3:3 by the loader).
 */
export interface SkillLabJobBody {
  type: "eval" | "train" | "taskgen";
  skill_source:
    | { kind: "registry"; record_id: string }
    | { kind: "registry"; record_ids: string[] }
    | { kind: "upload"; staging_id: string; index: number };
  /** required for eval/train; for taskgen it names the expansion target */
  taskset_id?: string;
  split?: string;
  /** taskgen expansion only: the split the generated tasks will extend */
  target_split?: string;
  /**
   * taskgen only: staged task-asset tokens the generation agent authors
   * against. The agent reads them at `data/<name>` and names the ones a task
   * needs; the backend turns those names into asset descriptors on import.
   */
  attachments?: { staged_asset: string }[];
  params?: Partial<SkillLabJobParams>;
}

/** `GET /jobs/{id}/results` for a taskgen job (eval jobs return SkillLabJobResults). */
export interface SkillLabTaskgenResults {
  type: "taskgen";
  count: number;
  tasks: SkillLabTask[];
  summary: Record<string, unknown>;
}

/**
 * One judged task. `score_valid === false` marks an infrastructure failure (the
 * rollout or the judge never produced a verdict) — those rows are counted as
 * `invalid` and excluded from the pass-rate denominator, never scored as zero.
 */
export interface SkillLabResultRow {
  id: string;
  task_type: string | null;
  hard: number | null;
  soft: number | null;
  score_valid: boolean | null;
  duration_s: number | null;
  judge_status: string | null;
  judge_reason: string | null;
  judge_error: string | null;
  error: string | null;
  usage: Record<string, unknown> | null;
  judge_usage: Record<string, unknown> | null;
  /** Excerpted server-side. */
  response: string;
  artifacts: { path: string | null; size: number | null   /** the host judge CLI this row's failure blames, when that is the cause */
  judge_prerequisite?: string | null;
}[];
}

export interface SkillLabJobResults {
  summary: {
    tasks: number;
    passed: number;
    invalid: number;
    pass_rate: number;
    soft_mean: number;
    duration_s: number;
    /** host judge CLIs a judge failure named as missing — an operator-fixable
     *  prerequisite rather than a bad task, stated once for the whole run */
    judge_prerequisite_missing: string[];
  };
  rows: SkillLabResultRow[];
}

/** `GET /jobs/{id}/artifacts` returns a directory listing or one file's body. */
export type SkillLabArtifactListing =
  | { kind: "dir"; path: string; dirs: string[]; files: { name: string; size: number }[] }
  | { kind: "text"; path: string; size: number; truncated: boolean; content: string }
  | { kind: "binary"; path: string; size: number };

/**
 * One optimizer step, straight out of the trainer's history record. `action`
 * carries the gate verdict as a substring ("accept" / "reject" / "skip…"), and
 * a null `selection_hard` means the step never reached the gate.
 */
export interface SkillLabTrainStep {
  step: number | null;
  epoch: number | null;
  action: string | null;
  selection_hard: number | null;
  selection_soft: number | null;
  current_score: number | null;
  best_score: number | null;
  best_step: number | null;
  skill_len: number | null;
  wall_time_s: number | null;
  gate_reasons: unknown;
  excluded_failures: unknown;
}

/**
 * `GET /jobs/{id}/train-summary` — readable MID-RUN: `steps` grows as the
 * trainer appends to history.json, while `finished` (and the totals/test
 * scores read from summary.json) only turn real at the end.
 */
export interface SkillLabTrainSummary {
  steps: SkillLabTrainStep[];
  finished: boolean;
  /** score of the seed skill on the val split; only known once finished */
  baseline_selection_hard: number | null;
  best_step: number | null;
  best_score: number | null;
  test_scores: { baseline: number | null; final: number | null };
  totals: {
    steps: number | null;
    accepts: number | null;
    rejects: number | null;
    skips: number | null;
    wall_time_s: number | null;
  };
}

/** `GET /jobs/{id}/diff` — SEED (skill_v0000.md) vs BEST (best_skill.md). */
export interface SkillLabSkillDiff {
  seed: string;
  best: string;
  /** false means no edit was ever accepted — publishing is refused */
  changed: boolean;
  diff: string;
}

/** `POST /jobs/{id}/publish` result; the update settles the record into DRAFT. */
export interface SkillLabPublishResult {
  record_id: string;
  name: string | null;
  new_version: string;
  status_before: string;
  status_after: string;
  reapproved: boolean;
}

/** One recommend-stage generator (`GET /api/experiments/providers`). */
export interface RecommendProviderInfo {
  id: string;
  label: string;
  /** Needs a pinned evaluation run (scored evidence) — the rolling window has none. */
  requires_source: boolean;
  supports: string[];
  /** Empty for AgentCore: the job picks its own judge; the model picker is hidden. */
  models: { model_id: string; label: string }[];
  default_model_id: string | null;
}

export const api = {
  authStatus: () => request<AuthStatus>("/api/auth/status"),
  /** `POST /api/eval/runs/{id}/stop` (202). With a batch on AWS: StopBatchEvaluation,
   *  the row follows STOPPING → STOPPED and comes back `stop_requested`; a queued run
   *  is cancelled locally and returns `stopped` at once. Terminal runs → 409
   *  `run.not_active`. */
  stopEvaluationRun: (runId: string) =>
    request<EvaluationRunInfo>(`/api/eval/runs/${encodeURIComponent(runId)}/stop`, {
      method: "POST",
    }),
  experimentProviders: () =>
    request<{ providers: RecommendProviderInfo[] }>("/api/experiments/providers"),
  login: (username: string, password: string) =>
    request<AuthLoginResult>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  register: (username: string, email: string, password: string) =>
    request<RegisterResult>("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, email, password }),
    }),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  listUsers: (params: {
    q?: string;
    status?: UserStatusFilter;
    limit?: number;
    offset?: number;
  } = {}) => {
    const query = new URLSearchParams();
    if (params.q) query.set("q", params.q);
    if (params.status && params.status !== "all") query.set("status", params.status);
    if (params.limit !== undefined) query.set("limit", String(params.limit));
    if (params.offset !== undefined) query.set("offset", String(params.offset));
    const suffix = query.toString();
    return request<UserListResult>(`/api/users${suffix ? `?${suffix}` : ""}`);
  },
  userStats: () => request<UserStats>("/api/users/stats"),
  listWorkspaces: () => request<WorkspaceListResult>("/api/workspaces"),
  /** The hub's own account and role — what a spoke's trust policy must name. */
  getHubIdentity: () =>
    request<{ account_id: string; caller_arn: string; role_arn: string }>(
      "/api/workspaces/hub-identity",
    ),
  createWorkspace: (body: {
    id: string;
    name: string;
    account_id: string;
    region: string;
    /** both together, or neither: a cross-account workspace */
    role_arn?: string;
    external_id?: string;
  }) =>
    request<Workspace>("/api/workspaces", { method: "POST", body: JSON.stringify(body) }),
  /** Probe an AssumeRole before registering anything; writes nothing. */
  preflightWorkspace: (body: {
    account_id: string;
    region: string;
    role_arn: string;
    external_id: string;
  }) =>
    request<WorkspacePreflightResult>("/api/workspaces/preflight", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  patchWorkspace: (id: string, body: { name: string }) =>
    request<Workspace>(`/api/workspaces/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteWorkspace: (id: string) =>
    request<{ deleted: boolean; workspace_id: string }>(`/api/workspaces/${id}`, {
      method: "DELETE",
    }),
  /**
   * Delete a failed or never-bootstrapped registration, ledger rows and all.
   *
   * `dryRun` runs the same guardrails and reports what would go without
   * deleting anything — the confirm dialog calls it on open, so a purge that
   * would be refused (the workspace turned READY, an agent appeared) says so
   * before the operator confirms rather than after.
   */
  purgeWorkspace: (id: string, opts: { dryRun?: boolean } = {}) =>
    request<WorkspacePurgeResult>(
      `/api/workspaces/${id}/purge${opts.dryRun ? "?dry_run=true" : ""}`,
      { method: "POST" },
    ),
  listWorkspaceGrants: (
    id: string,
    params: {
      q?: string;
      granted?: WorkspaceGrantFilter;
      limit?: number;
      offset?: number;
    } = {},
  ) => {
    const search = new URLSearchParams();
    if (params.q) search.set("q", params.q);
    if (params.granted && params.granted !== "all") search.set("granted", params.granted);
    if (params.limit != null) search.set("limit", String(params.limit));
    if (params.offset) search.set("offset", String(params.offset));
    const query = search.toString();
    return request<WorkspaceGrants>(
      `/api/workspaces/${id}/grants${query ? `?${query}` : ""}`,
    );
  },
  /**
   * Grant or revoke a workspace for several accounts at once.
   *
   * The workspace-side bulk shape; `updateUser({workspaces})` remains the
   * per-user full replacement. Both write only the grant table.
   */
  updateWorkspaceGrants: (id: string, body: { grant?: string[]; revoke?: string[] }) =>
    request<WorkspaceGrantsResult>(`/api/workspaces/${id}/grants`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  bootstrapWorkspace: (id: string) =>
    request<WorkspaceBootstrapAck>(`/api/workspaces/${id}/bootstrap`, { method: "POST" }),
  /** The latest bootstrap run, so a browser that did not start it can watch. */
  getWorkspaceBootstrap: (id: string) =>
    request<{
      workspace_id: string;
      bootstrap_status: string;
      job: { id: string; status: string; stages: StageInfo[] } | null;
    }>(`/api/workspaces/${id}/bootstrap`),
  /**
   * A job of a workspace other than the current selection.
   *
   * `GET /api/jobs/{id}` is workspace-scoped and 404s across workspaces, so
   * watching a bootstrap has to name its target explicitly instead of letting
   * the global header stamp the selected one.
   */
  getWorkspaceJob: (jobId: string, workspaceId: string) =>
    request<WorkspaceBootstrapJob>(`/api/jobs/${jobId}`, {
      headers: { "X-Workspace": workspaceId },
    }),
  updateUser: (id: string, patch: UserPatchBody) =>
    request<ConsoleUser>(`/api/users/${id}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  deleteUser: (id: string) =>
    request<{ ok: boolean }>(`/api/users/${id}`, { method: "DELETE" }),
  createAgent: (spec: AgentSpecInput) =>
    request<{ agent: AgentInfo; job_id: string; deployment_id: string }>("/api/agents", {
      method: "POST",
      body: JSON.stringify(spec),
    }),
  listAgents: () => request<{ agents: AgentInfo[] }>("/api/agents"),
  listChatSessions: (agentId: string) =>
    request<{ sessions: ChatSessionInfo[] }>(`/api/chat/${encodeURIComponent(agentId)}/sessions`),
  stopChatSession: (agentId: string, sessionId: string) =>
    request<ChatSessionStopResult>(
      `/api/chat/${encodeURIComponent(agentId)}/sessions/${encodeURIComponent(sessionId)}/stop`,
      { method: "POST" },
    ),
  discoverRuntimes: () =>
    request<RuntimeDiscoveryResponse>("/api/agents/discovery"),
  importRuntimes: (runtimeIds: string[], harnessIds: string[] = []) =>
    request<RuntimeImportResult>("/api/agents/discovery/import", {
      method: "POST",
      body: JSON.stringify({ runtime_ids: runtimeIds, harness_ids: harnessIds }),
    }),
  convertAgent: (id: string) =>
    request<{ agent: AgentInfo; job_id: string; deployment_id: string }>(
      `/api/agents/${id}/convert`,
      { method: "POST" },
    ),
  redeployAgent: (id: string, spec: AgentSpecInput) =>
    request<{ agent: AgentInfo; job_id: string; deployment_id: string }>(
      `/api/agents/${id}/redeploy`,
      { method: "POST", body: JSON.stringify(spec) },
    ),
  getOverview: () => request<OverviewInfo>("/api/overview"),
  overviewOnlineQuality: () => request<OnlineQuality>("/api/overview/online-quality"),
  getAgent: (id: string) => request<AgentInfo>(`/api/agents/${id}`),
  agentVersions: (id: string) => request<AgentVersionsInfo>(`/api/agents/${id}/versions`),
  getJob: (id: string) => request<JobInfo>(`/api/jobs/${id}`),
  listRuntimeCanaries: () =>
    request<{ canaries: RuntimeCanaryInfo[] }>("/api/runtime-canaries"),
  getRuntimeCanary: (id: string) =>
    request<RuntimeCanaryInfo>(`/api/runtime-canaries/${id}`),
  createRuntimeCanary: (input: {
    agent_id: string;
    candidate: {
      system_prompt?: string;
      tool_description_overrides?: Record<string, string>;
      code?: string;
    };
    source_experiment_id?: string;
  }) =>
    request<RuntimeCanaryInfo>("/api/runtime-canaries", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  runtimeCanaryAction: (
    id: string,
    input: {
      action: string;
      dataset_id?: string;
      allow_non_significant?: boolean;
    },
  ) =>
    request<{ canary: RuntimeCanaryInfo }>(`/api/runtime-canaries/${id}/action`, {
      method: "POST",
      body: JSON.stringify(input),
    }),
  invokeAgent: (id: string, prompt: string, sessionId?: string) =>
    request<{ text: string; session_id: string; latency_ms: number }>(
      `/api/agents/${id}/invoke`,
      { method: "POST", body: JSON.stringify({ prompt, session_id: sessionId }) },
    ),
  deleteAgent: (id: string) =>
    request<{ deleted: boolean; agent_id: string; aws_resource_deleted: boolean }>(
      `/api/agents/${id}`,
      { method: "DELETE" },
    ),
  inspectSkillZip: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return requestForm<{ staging_id: string; skills: InspectedSkill[] }>(
      "/api/registry/skills/inspect",
      form,
    );
  },
  inspectSkillGit: (url: string, ref?: string, subdir?: string) =>
    request<{ staging_id: string; skills: InspectedSkill[] }>("/api/registry/skills/inspect", {
      method: "POST",
      body: JSON.stringify({ source: { kind: "git", url, ref, subdir } }),
    }),
  attachSkillSources: (stagingId: string, selections: { index: number }[]) =>
    request<{ skills: AttachedSkill[] }>("/api/agent-skills/import", {
      method: "POST",
      body: JSON.stringify({ staging_id: stagingId, selections }),
    }),
  listGovernanceGateways: (force = false) =>
    request<GovernanceGatewayListResponse>(
      `/api/governance/gateways${force ? "?refresh=true" : ""}`,
    ),
  getGovernanceGateway: (gatewayId: string) =>
    request<GovernanceGatewayDetail>(governanceGatewayPath(gatewayId)),
  manageGovernanceGateway: (gatewayId: string) =>
    request<GovernanceManageResult>(`${governanceGatewayPath(gatewayId)}/manage`, {
      method: "POST",
    }),
  unmanageGovernanceGateway: (gatewayId: string) =>
    request<GovernanceManageResult>(`${governanceGatewayPath(gatewayId)}/manage`, {
      method: "DELETE",
    }),
  governanceRegistryPreview: (gatewayId: string) =>
    request<GovernanceRegistryPreview>(
      `${governanceGatewayPath(gatewayId)}/registry-preview`,
    ),
  importGovernanceRegistry: (
    gatewayId: string,
    input: GovernanceRegistryImportRequest,
  ) =>
    request<GovernanceRegistryImportResult>(
      `${governanceGatewayPath(gatewayId)}/registry-import`,
      { method: "POST", body: JSON.stringify(input) },
    ),
  retireGovernanceLegacyRecords: (
    gatewayId: string,
    input: GovernanceRetireLegacyRequest,
  ) =>
    request<{ retired: string[]; skipped: string[] }>(
      `${governanceGatewayPath(gatewayId)}/retire-legacy-records`,
      { method: "POST", body: JSON.stringify(input) },
    ),
  attachGovernanceEngine: (gatewayId: string, input: GovernanceEngineRequest) =>
    request<{ operation: GovernanceOperation }>(
      `${governanceGatewayPath(gatewayId)}/engine`,
      {
        method: "POST",
        body: JSON.stringify(input),
      },
    ).then((result) => result.operation),
  listGovernancePolicies: (gatewayId: string) =>
    request<GovernancePolicyListResponse>(
      `${governanceGatewayPath(gatewayId)}/policies`,
    ),
  createGovernancePolicy: (
    gatewayId: string,
    input: GovernancePolicyCreateRequest,
  ) =>
    request<{ operation: GovernanceOperation }>(
      `${governanceGatewayPath(gatewayId)}/policies`,
      {
        method: "POST",
        body: JSON.stringify(input),
      },
    ).then((result) => result.operation),
  updateGovernancePolicy: (
    gatewayId: string,
    policyId: string,
    input: GovernancePolicyUpdateRequest,
  ) =>
    request<{ operation: GovernanceOperation }>(
      `${governanceGatewayPath(gatewayId)}/policies/${encodeURIComponent(policyId)}`,
      { method: "PUT", body: JSON.stringify(input) },
    ).then((result) => result.operation),
  deleteGovernancePolicy: (
    gatewayId: string,
    policyId: string,
    input: GovernancePolicyDeleteRequest,
  ) =>
    request<{ operation: GovernanceOperation }>(
      `${governanceGatewayPath(gatewayId)}/policies/${encodeURIComponent(policyId)}`,
      { method: "DELETE", body: JSON.stringify(input) },
    ).then((result) => result.operation),
  promoteGovernancePolicy: (
    gatewayId: string,
    policyId: string,
    input: GovernancePolicyTransitionRequest,
  ) =>
    request<{ operation: GovernanceOperation }>(
      `${governanceGatewayPath(gatewayId)}/policies/${encodeURIComponent(policyId)}/promote`,
      { method: "POST", body: JSON.stringify(input) },
    ).then((result) => result.operation),
  rollbackGovernancePolicy: (
    gatewayId: string,
    policyId: string,
    input: GovernancePolicyTransitionRequest,
  ) =>
    request<{ operation: GovernanceOperation }>(
      `${governanceGatewayPath(gatewayId)}/policies/${encodeURIComponent(policyId)}/rollback`,
      { method: "POST", body: JSON.stringify(input) },
    ).then((result) => result.operation),
  setGovernanceGatewayMode: (
    gatewayId: string,
    input: GovernanceGatewayModeRequest,
  ) =>
    request<{ operation: GovernanceOperation }>(
      `${governanceGatewayPath(gatewayId)}/mode`,
      {
        method: "POST",
        body: JSON.stringify(input),
      },
    ).then((result) => result.operation),
  startGovernanceGeneration: (
    gatewayId: string,
    input: GovernanceGenerationRequest,
  ) =>
    request<{
      operation: GovernanceOperation;
      generation_id: string;
      status: string;
    }>(`${governanceGatewayPath(gatewayId)}/generations`, {
      method: "POST",
      body: JSON.stringify(input),
    }).then((result) => ({
      id: result.generation_id,
      status: result.status,
      status_reasons: [],
      findings: null,
      assets: [],
    })),
  getGovernanceGeneration: (gatewayId: string, generationId: string) =>
    request<GovernanceGeneration>(
      `${governanceGatewayPath(gatewayId)}/generations/${encodeURIComponent(generationId)}`,
    ),
  runGovernancePolicyTest: (input: GovernancePolicyTestRequest) =>
    request<GovernancePolicyTestResult>("/api/governance/policy-test", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  governanceDecisions: (
    gatewayId: string,
    range: GovernanceEvidenceRange,
    policyId?: string,
    force = false,
  ) => {
    const query = new URLSearchParams({ range });
    if (policyId) query.set("policy_id", policyId);
    if (force) query.set("force", "true");
    return request<GovernanceDecisionResponse>(
      `${governanceGatewayPath(gatewayId)}/decisions?${query.toString()}`,
    );
  },
  listGovernanceRateLimits: (gatewayId: string) =>
    request<GovernanceRateLimitListResponse>(
      `${governanceGatewayPath(gatewayId)}/rate-limits`,
    ),
  createGovernanceRateLimit: (gatewayId: string, input: GovernanceRateLimitCreateRequest) =>
    request<GovernanceRateLimit>(`${governanceGatewayPath(gatewayId)}/rate-limits`, {
      method: "POST",
      body: JSON.stringify(input),
    }),
  updateGovernanceRateLimit: (
    gatewayId: string,
    rateLimitId: string,
    input: GovernanceRateLimitUpdateRequest,
  ) =>
    request<GovernanceRateLimit>(
      `${governanceGatewayPath(gatewayId)}/rate-limits/${encodeURIComponent(rateLimitId)}`,
      { method: "PUT", body: JSON.stringify(input) },
    ),
  deleteGovernanceRateLimit: (gatewayId: string, rateLimitId: string) =>
    request<GovernanceRateLimitDeleteResult>(
      `${governanceGatewayPath(gatewayId)}/rate-limits/${encodeURIComponent(rateLimitId)}`,
      { method: "DELETE" },
    ),
  governanceAudit: (gatewayId: string) =>
    request<GovernanceAuditResponse>(`${governanceGatewayPath(gatewayId)}/audit`),
  governanceOperation: (operationId: string) =>
    request<{ operation: GovernanceOperation }>(
      `/api/governance/operations/${encodeURIComponent(operationId)}`,
    ).then((result) => result.operation),
  governanceToolCatalog: () => request<GovernanceToolCatalog>("/api/tools"),
  runCodeInterpreterDemo: (code: string) =>
    request<CodeInterpreterDemoResult>("/api/demos/code-interpreter", {
      method: "POST",
      body: JSON.stringify({ code }),
    }),
  browserDemoOptions: () =>
    request<BrowserDemoOptions>("/api/demos/browser/options"),
  runBrowserDemo: (input: BrowserDemoRequest) =>
    request<BrowserDemoResult>("/api/demos/browser", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  stopBrowserDemo: (sessionId: string) =>
    request<{
      session_id: string;
      stopped: boolean;
      profile_saved: boolean | null;
    }>(
      `/api/demos/browser/${encodeURIComponent(sessionId)}`,
      { method: "DELETE" },
    ),
  memoryOverview: () => request<MemoryOverview>("/api/memory/overview"),
  memoryActors: (nextToken?: string | null) =>
    request<MemoryPage<MemoryActor>>(
      `/api/memory/actors${memoryQuery({ next_token: nextToken })}`,
    ),
  memorySessions: (actorId: string, nextToken?: string | null) =>
    request<MemoryPage<MemorySessionRow>>(
      `/api/memory/sessions${memoryQuery({ actor_id: actorId, next_token: nextToken })}`,
    ),
  memoryEvents: (actorId: string, sessionId: string, nextToken?: string | null) =>
    request<MemoryPage<MemoryEvent>>(
      `/api/memory/events${memoryQuery({
        actor_id: actorId,
        session_id: sessionId,
        next_token: nextToken,
      })}`,
    ),
  memoryNamespaces: (actorId: string) =>
    request<{ items: MemoryNamespace[] }>(
      `/api/memory/namespaces${memoryQuery({ actor_id: actorId })}`,
    ),
  memoryRecords: (
    params: { namespace?: string; actor_id?: string; strategy_id?: string },
    nextToken?: string | null,
  ) =>
    request<MemoryRecordPage>(
      `/api/memory/records${memoryQuery({ ...params, next_token: nextToken })}`,
    ),
  memorySearchRecords: (input: MemorySearchInput) =>
    request<MemoryRecordPage>("/api/memory/records/search", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  memoryResources: () => request<MemoryResourceList>("/api/memory/resources"),
  memoryResourceCreate: (input: MemoryResourceCreateInput) =>
    request<MemoryResourceDetail>("/api/memory/resources", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  memoryResource: (memoryId: string) =>
    request<MemoryResourceDetail>(
      `/api/memory/resources/${encodeURIComponent(memoryId)}`,
    ),
  memoryResourceUpdate: (memoryId: string, input: MemoryResourceUpdateInput) =>
    request<MemoryResourceDetail>(
      `/api/memory/resources/${encodeURIComponent(memoryId)}`,
      { method: "PUT", body: JSON.stringify(input) },
    ),
  memoryResourceDelete: (memoryId: string) =>
    request<{ deleted: boolean; id: string }>(
      `/api/memory/resources/${encodeURIComponent(memoryId)}`,
      { method: "DELETE" },
    ),
  // NOTE: `GET /api/memory/extraction-jobs` exists on the backend but is not
  // surfaced in the console — the AWS list only ever returns FAILED jobs
  // (retry backlog), which reads as "nothing extracted" to an operator.
  onlineEvalReports: (configId: string) =>
    request<OnlineEvalReports>(`/api/eval/online/${encodeURIComponent(configId)}/reports`),
  onlineEvalReport: (configId: string, batchId: string) =>
    request<OnlineEvalReportDetail>(
      `/api/eval/online/${encodeURIComponent(configId)}/reports/${encodeURIComponent(batchId)}`,
    ),
  /** Starts an on-demand insights report over the config's sampled sessions
   *  in `range` (agent-owned insights configs only; queued like any run). */
  onlineEvalRunReport: (configId: string, range: OnlineEvalRange) =>
    request<OnlineEvalRunReportAck>(
      `/api/eval/online/${encodeURIComponent(configId)}/reports`,
      { method: "POST", body: JSON.stringify({ range }) },
    ),
  obsDashboard: (range: string, force = false) =>
    request<ObsDashboard>(`/api/observability/dashboard?${obsQuery(range, force)}`),
  obsTraces: (range: string, force = false) =>
    request<ObsTraces>(`/api/observability/traces?${obsQuery(range, force)}`),
  obsTrace: (traceId: string, range: string, force = false) =>
    request<ObsTraceDetail>(
      `/api/observability/traces/${encodeURIComponent(traceId)}?${obsQuery(range, force)}`,
    ),
  obsSessions: (range: string, force = false) =>
    request<ObsSessions>(`/api/observability/sessions?${obsQuery(range, force)}`),
  obsSession: (sessionId: string, range: string, force = false) =>
    request<ObsSessionDetail>(
      `/api/observability/sessions/${encodeURIComponent(sessionId)}?${obsQuery(range, force)}`,
    ),
  obsRefreshPrices: () =>
    request<{ prices: Record<string, unknown>; meta: Required<ObsPricesMeta> }>(
      "/api/observability/prices/refresh",
      { method: "POST" },
    ),
  skillLabTaskAssetsUpload: (files: File[]) => {
    const form = new FormData();
    for (const file of files) form.append("files", file);
    return requestForm<SkillLabTaskAssetUploadResponse>(
      "/api/skill-lab/task-assets",
      form,
    );
  },
  skillLabStatus: () => request<SkillLabStatus>("/api/skill-lab/status"),
  skillLabTasksets: () => request<SkillLabTasksetInfo[]>("/api/skill-lab/tasksets"),
  skillLabTasksetGet: (id: string, full = false) =>
    request<SkillLabTasksetDetail>(
      `/api/skill-lab/tasksets/${encodeURIComponent(id)}${full ? "?full=true" : ""}`,
    ),
  skillLabTasksetCreate: (body: SkillLabTasksetBody) =>
    request<SkillLabTasksetInfo>("/api/skill-lab/tasksets", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  /** Full replace: `tasks_by_split` must carry every split that should exist. */
  skillLabTasksetUpdate: (
    id: string,
    body: { name?: string; description?: string; tasks_by_split: Record<string, SkillLabTask[]> },
  ) =>
    request<SkillLabTasksetInfo>(`/api/skill-lab/tasksets/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),
  skillLabTasksetDelete: (id: string) =>
    request<{ ok: boolean }>(`/api/skill-lab/tasksets/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  skillLabJobs: (type?: "eval" | "train" | "taskgen") =>
    request<SkillLabJobInfo[]>(`/api/skill-lab/jobs${type ? `?type=${type}` : ""}`),
  skillLabJobCreate: (body: SkillLabJobBody) =>
    request<SkillLabJobInfo>("/api/skill-lab/jobs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  skillLabJobGet: (id: string) =>
    request<SkillLabJobInfo>(`/api/skill-lab/jobs/${encodeURIComponent(id)}`),
  skillLabJobCancel: (id: string) =>
    request<SkillLabJobInfo>(`/api/skill-lab/jobs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
    }),
  skillLabJobDelete: (id: string) =>
    request<{ ok: boolean }>(`/api/skill-lab/jobs/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  /**
   * Byte-offset tail: append `content`, then poll again with `next_offset`.
   * `eof: false` means the server capped the chunk — keep polling to catch up.
   */
  skillLabJobLog: (id: string, offset = 0) =>
    request<{ content: string; next_offset: number; eof: boolean }>(
      `/api/skill-lab/jobs/${encodeURIComponent(id)}/log?offset=${offset}`,
    ),
  skillLabJobResults: (id: string) =>
    request<SkillLabJobResults>(`/api/skill-lab/jobs/${encodeURIComponent(id)}/results`),
  skillLabTaskgenResults: (id: string) =>
    request<SkillLabTaskgenResults>(`/api/skill-lab/jobs/${encodeURIComponent(id)}/results`),
  /** Save a succeeded taskgen job's reviewed tasks as a NEW single-mode task set. */
  skillLabTaskgenImport: (id: string, name: string) =>
    request<{ job: SkillLabJobInfo; taskset: SkillLabTasksetInfo }>(
      `/api/skill-lab/jobs/${encodeURIComponent(id)}/import-taskset`,
      { method: "POST", body: JSON.stringify({ name }) },
    ),
  /** Append a succeeded expansion job's tasks to its target task set/split. */
  skillLabTaskgenApply: (id: string) =>
    request<{ job: SkillLabJobInfo; taskset: SkillLabTasksetInfo }>(
      `/api/skill-lab/jobs/${encodeURIComponent(id)}/apply-expansion`,
      { method: "POST" },
    ),
  /** 404 `skill_lab.results_pending` until the first optimizer step lands. */
  skillLabJobTrainSummary: (id: string) =>
    request<SkillLabTrainSummary>(`/api/skill-lab/jobs/${encodeURIComponent(id)}/train-summary`),
  skillLabJobDiff: (id: string) =>
    request<SkillLabSkillDiff>(`/api/skill-lab/jobs/${encodeURIComponent(id)}/diff`),
  /** Train only: re-runs the same command, which continues from the last step. */
  skillLabJobResume: (id: string) =>
    request<SkillLabJobInfo>(`/api/skill-lab/jobs/${encodeURIComponent(id)}/resume`, {
      method: "POST",
    }),
  skillLabJobPublish: (id: string, reapprove: boolean) =>
    request<SkillLabPublishResult>(`/api/skill-lab/jobs/${encodeURIComponent(id)}/publish`, {
      method: "POST",
      body: JSON.stringify({ reapprove }),
    }),
  skillLabJobArtifacts: (id: string, path = "") =>
    request<SkillLabArtifactListing>(
      `/api/skill-lab/jobs/${encodeURIComponent(id)}/artifacts?path=${encodeURIComponent(path)}`,
    ),
  /**
   * Raw artifact bytes. Fetched (not linked): an `<a href>` navigation skips the
   * `window.fetch` wrapper that stamps `X-Workspace`, so the backend would look
   * the job up in the fallback workspace and 404 for everyone whose selection is
   * not the default one.
   */
  skillLabJobArtifactRaw: async (id: string, path: string) => {
    const url = `/api/skill-lab/jobs/${encodeURIComponent(id)}/artifacts/raw?path=${encodeURIComponent(path)}`;
    const res = await fetch(url);
    if (!res.ok) return parseResponse<never>(url, res);
    return res.blob();
  },
};

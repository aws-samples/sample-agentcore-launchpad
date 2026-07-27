/** Typed client for the Launchpad backend. */

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
  method: "harness" | "zip_runtime" | "container" | "studio";
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
  created_at: string | null;
  updated_at: string | null;
  deployment?: DeploymentInfo;
  deployments?: DeploymentInfo[];
  revision?: number;
}

export interface RuntimeCanaryMetric {
  label: string;
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
    setup?: {
      gateway_id: string;
      gateway_arn: string;
      gateway_url: string;
      test_name: string;
      ab_test_id: string;
      ramp_stage: number;
      weights: Record<string, number>;
      v_current: string;
      v_candidate: string;
      stable_endpoint: string;
      treatment_endpoint: string;
      runtime_id: string;
      champion: {
        target_name: string;
        target_id: string;
        online_eval_id: string;
      };
      challenger: {
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

export interface AgentSpecInput {
  name: string;
  method: string;
  model_id?: string;
  system_prompt: string;
  tool_description_overrides?: Record<string, string>;
  tools?: { type: string; name: string; config?: Record<string, unknown> }[];
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

async function parseResponse<T>(path: string, res: Response): Promise<T> {
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    if (res.status === 401 && !path.startsWith("/api/auth/")) {
      window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT));
    }
    const env = (body ?? {}) as { code?: string; message?: string; detail?: unknown };
    throw new ApiError(env.code ?? `http.${res.status}`, env.message ?? res.statusText, env.detail);
  }
  return body as T;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  return parseResponse<T>(path, res);
}

/** multipart POST — the browser sets the boundary Content-Type itself. */
async function requestForm<T>(path: string, form: FormData): Promise<T> {
  const res = await fetch(path, { method: "POST", body: form });
  return parseResponse<T>(path, res);
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
  name: string;
  status: string;
  status_reasons: string[];
  updated_at: string | null;
  mode: GovernanceGatewayMode | null;
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

export interface GovernancePolicyDecision {
  at: string;
  gateway_id: string;
  gateway_arn: string;
  engine_id: string | null;
  policy_id: string | null;
  principal: string;
  action: string;
  outcome: "ALLOW" | "DENY";
  engine_mode: GovernanceGatewayMode | null;
  policy_mode: GovernancePolicyMode | null;
  trace_id: string | null;
  session_id: string | null;
  source: "aws";
}

export interface GovernanceDecisionResponse {
  range: GovernanceEvidenceRange;
  decisions: GovernancePolicyDecision[];
  count: number;
  available: boolean;
  unavailable_reason: string | null;
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
  source?: "chat" | "eval";
  origin?: "memory" | "logs";
  run_id?: string | null;
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
  cache: ObsCache;
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

export interface AuthStatus {
  auth_required: boolean;
  authenticated: boolean;
  username: string | null;
}

export interface AuthLoginResult extends AuthStatus {
  ok: boolean;
  expires_at: number | null;
}

export const api = {
  authStatus: () => request<AuthStatus>("/api/auth/status"),
  login: (username: string, password: string) =>
    request<AuthLoginResult>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  createAgent: (spec: AgentSpecInput) =>
    request<{ agent: AgentInfo; job_id: string; deployment_id: string }>("/api/agents", {
      method: "POST",
      body: JSON.stringify(spec),
    }),
  listAgents: () => request<{ agents: AgentInfo[] }>("/api/agents"),
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
  getAgent: (id: string) => request<AgentInfo>(`/api/agents/${id}`),
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
    request<{ deleted: boolean }>(`/api/agents/${id}`, { method: "DELETE" }),
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
  // NOTE: `GET /api/memory/extraction-jobs` exists on the backend but is not
  // surfaced in the console — the AWS list only ever returns FAILED jobs
  // (retry backlog), which reads as "nothing extracted" to an operator.
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
};

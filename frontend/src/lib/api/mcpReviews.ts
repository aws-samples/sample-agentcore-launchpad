/**
 * Typed client of the MCP tool-review API (projects §15.6, backend
 * `routers/mcp_reviews.py`). Every tool list a hosted MCP server presents is
 * snapshotted; until a member holding `mcp.approve` — never the deployer — approves
 * it, the server's Gateway target is withheld. Workspace + project routes: they follow
 * the shared `X-Workspace` / `X-Project` selection like every other agent route.
 */
import { getJson, sendJson } from "../api";

const enc = encodeURIComponent;

export type McpReviewStatus = "reviewing" | "approved" | "rejected" | "superseded";
/** `none` before the server's first tool list */
export type McpGateState = "none" | "reviewing" | "approved" | "rejected";
export type McpReviewSource = "deploy" | "drift" | "baseline" | "rollback";
export type McpHintKind =
  | "dangerous_verb"
  | "write_name"
  | "missing_description"
  | "injection_phrase"
  | "url"
  | "cross_tool_reference"
  | "long_description";

export interface McpReviewedTool {
  name: string;
  description: string;
  input_schema: Record<string, unknown>;
}

export interface McpToolDiff {
  added: string[];
  removed: string[];
  description_changed: string[];
  schema_changed: string[];
}

export interface McpHint {
  tool: string;
  kind: McpHintKind;
  detail: string;
}

export interface McpReview {
  id: string;
  agent_id: string;
  version: string | null;
  target_name: string;
  tools_hash: string;
  tool_count: number;
  base_review_id: string | null;
  diff: McpToolDiff;
  hints: McpHint[];
  status: McpReviewStatus;
  source: McpReviewSource;
  requested_by: string;
  decided_by: string | null;
  decided_at: string | null;
  note: string;
  /** after an approval the withheld target is re-created in the background */
  publish_status: "publishing" | "published" | "failed" | null;
  publish_error: string | null;
  created_at: string | null;
}

/** `GET …/reviews/{id}`: the full list, and the approved list it is compared with */
export interface McpReviewDetail extends McpReview {
  tools: McpReviewedTool[];
  base_tools: McpReviewedTool[] | null;
}

export interface McpGate {
  state: McpGateState;
  open_review_id: string | null;
  approved_review_id: string | null;
  approved_hash: string | null;
}

export interface McpReviewList extends McpGate {
  agent_id: string;
  reviews: McpReview[];
}

export interface McpDriftCheck extends McpGate {
  checked: boolean;
  drift: boolean;
  reason?: string;
  review_id?: string;
}

/** `/api/mcp-servers/{agentId}/reviews[/{...rest}]`, each segment encoded. */
export function mcpReviewPath(agentId: string, ...rest: string[]): string {
  return [`/api/mcp-servers/${enc(agentId)}/reviews`, ...rest.map(enc)].join("/");
}

export const listMcpReviews = (agentId: string) => getJson<McpReviewList>(mcpReviewPath(agentId));

export const getMcpReview = (agentId: string, reviewId: string) =>
  getJson<McpReviewDetail>(mcpReviewPath(agentId, reviewId));

export const approveMcpReview = (agentId: string, reviewId: string, note = "") =>
  sendJson<McpReviewDetail>(mcpReviewPath(agentId, reviewId, "approve"), "POST", { note });

export const rejectMcpReview = (agentId: string, reviewId: string, note = "") =>
  sendJson<McpReviewDetail>(mcpReviewPath(agentId, reviewId, "reject"), "POST", { note });

/** Re-sync the live target and compare with the approved list (drift). */
export const checkMcpDrift = (agentId: string) => sendJson<McpDriftCheck>(mcpReviewPath(agentId, "check"), "POST");

/** Which tools a reviewer should read first: changed ones, then hinted ones. */
export function reviewFocus(review: Pick<McpReview, "diff" | "hints">): Set<string> {
  const d = review.diff;
  return new Set([...(d.added ?? []), ...(d.description_changed ?? []), ...(d.schema_changed ?? []), ...review.hints.map((h) => h.tool)]);
}

/** The change marker of one tool in a review (`+` added, `~` changed, `""` same). */
export function changeMark(diff: McpToolDiff, name: string): "+" | "~" | "" {
  if (diff.added?.includes(name)) return "+";
  if (diff.description_changed?.includes(name) || diff.schema_changed?.includes(name)) return "~";
  return "";
}

// ── server operations (projects §15.7, §15.8) ────────────────────────────────

export type McpHealthStatus = "healthy" | "drift" | "failing" | "withheld";

/** The last health check (hourly `mcp.health`, or `POST …/health`). */
export interface McpHealth {
  status?: McpHealthStatus;
  checked_at?: string;
  tool_count?: number | null;
  error?: string | null;
  consecutive_failures?: number;
  /** three failures in a row */
  alert?: boolean;
  review_id?: string | null;
  /** the check moved the target to the server's current endpoint */
  repointed?: boolean;
}

/** The last background operation of the server. */
export interface McpOperation {
  kind?: "rollback" | "scaffold";
  status?: "running" | "succeeded" | "failed";
  version?: string;
  actor?: string;
  error?: string | null;
  started_at?: string;
  completed_at?: string;
  results?: McpScaffoldResult[];
}

/** What `GET /api/agents/{id}/mcp` adds beyond `McpServerStatus` (lib/api.ts). */
export interface McpServerOps {
  /** the runtime endpoint the target calls: DEFAULT follows every version, `live` is pinned */
  qualifier?: "DEFAULT" | "live";
  live_version?: string | null;
  health?: McpHealth;
  operation?: McpOperation;
  /** approved tool lists per runtime version, newest first — rollback targets */
  rollback_versions?: { version: string; review_id: string; decided_at: string | null }[];
}

export interface McpScaffoldStep {
  name: string;
  action: "create" | "update" | "delete" | "keep";
  statement?: string;
  policy_id?: string;
}

export interface McpScaffoldPlan {
  gateway_id: string;
  gateway_name: string | null;
  engine_id: string | null;
  /** the Gateway's current mode */
  mode: "LOG_ONLY" | "ENFORCE" | null;
  steps: McpScaffoldStep[];
  approved_review_id: string | null;
  open_review_id: string | null;
}

export interface McpScaffoldResult {
  name: string;
  action: string;
  operation_id: string | null;
  status: string;
  error: string | null;
}

export interface McpScaffoldRequest {
  dry_run?: boolean;
  /** switch the Gateway after the policies (shared infrastructure) */
  mode?: "LOG_ONLY" | "ENFORCE";
  confirmation_name?: string;
  override_reason?: string;
  acknowledged_gateway_ids?: string[];
}

const serverPath = (agentId: string, ...rest: string[]) =>
  [`/api/mcp-servers/${enc(agentId)}`, ...rest].join("/");

/** Run one health check now (re-sync, drift, follow the live endpoint). */
export const checkMcpHealth = (agentId: string) =>
  sendJson<McpGate & { health: McpHealth }>(serverPath(agentId, "health"), "POST");

/** Return the target to an earlier approved version (background; see `operation`). */
export const rollbackMcpServer = (agentId: string, version?: string) =>
  sendJson<{ version: string; review_id: string; operation: McpOperation }>(
    serverPath(agentId, "rollback"),
    "POST",
    version ? { version } : {},
  );

/** Plan (`dry_run`) or apply the Cedar forbid / permit pair. */
export const scaffoldMcpPolicies = (agentId: string, body: McpScaffoldRequest) =>
  sendJson<{ plan: McpScaffoldPlan; operation?: McpOperation }>(serverPath(agentId, "policies", "scaffold"), "POST", body);

// ── Lambda and adopted tool targets (projects §15.12, §15.15) ─────────────────

export type LambdaToolStatus = "reviewing" | "publishing" | "live" | "rejected" | "failed";

/** One MCP tool definition as the Gateway takes it inline. */
export interface LambdaToolDefinition {
  name: string;
  description?: string;
  inputSchema: Record<string, unknown>;
  outputSchema?: Record<string, unknown>;
}

export interface LambdaToolTarget {
  id: string;
  kind: "lambda";
  gateway_id: string;
  target_id: string | null;
  target_name: string;
  lambda_arn: string;
  description: string;
  tool_names: string[];
  status: LambdaToolStatus;
  error: string | null;
  created_by: string;
  created_at: string | null;
  review: McpGate;
  tool_schema?: LambdaToolDefinition[];
  review_id?: string;
}

export interface LambdaTargetRequest {
  lambda_arn: string;
  target_name: string;
  tool_schema: LambdaToolDefinition[];
  description?: string;
}

export interface ExternalMcpTarget {
  target_id: string;
  target_name: string;
  endpoint: string;
  status: string | null;
  hosting: "external";
  /** set once brought under review in this project */
  tool_id: string | null;
  adopted_elsewhere: boolean;
  review: McpGate | null;
  health: McpHealth | null;
}

export const listLambdaTargets = () => getJson<{ targets: LambdaToolTarget[] }>("/api/gateway-tools/lambda");

export const createLambdaTarget = (body: LambdaTargetRequest) =>
  sendJson<LambdaToolTarget>("/api/gateway-tools/lambda", "POST", body);

export const deleteLambdaTarget = (id: string) =>
  sendJson<{ deleted: string }>(`/api/gateway-tools/lambda/${enc(id)}`, "DELETE");

export const listExternalTargets = () => getJson<{ targets: ExternalMcpTarget[] }>("/api/gateway-tools/external");

export const adoptExternalTarget = (targetId: string) =>
  sendJson<{ tool_id: string; target_id: string; target_name: string; review: McpGate }>(
    `/api/gateway-tools/external/${enc(targetId)}/adopt`,
    "POST",
  );

/** Why an unmanaged target cannot be adopted here: a bootstrap target, one bound to a
 *  Connection (managed under Connections), not a Lambda target, a schema kept in S3 or
 *  not a valid tool list, a function outside this workspace's account/region, or one
 *  another project already manages. `null` = adoptable. */
export type UnmanagedTargetReason = "system" | "connection" | "kind" | "schema" | "environment" | "elsewhere";

/** A target on the workspace Gateway neither the Lambda nor the externally hosted list
 *  covers — typically a Lambda target made by hand, or a bootstrap / OpenAPI one. */
export interface UnmanagedTarget {
  target_id: string;
  target_name: string;
  /** the `mcp` union member: lambda, openApiSchema, smithyModel, apiGateway, … */
  kind: string;
  status: string | null;
  description: string;
  lambda_arn: string | null;
  tool_names: string[];
  system: boolean;
  connection: string | null;
  adopted_elsewhere: boolean;
  reason: UnmanagedTargetReason | null;
}

/** `lambda` → "Lambda", `openApiSchema` → "OpenAPI", … (the target's `mcp` union member). */
export function targetKindLabel(kind: string): string {
  const labels: Record<string, string> = { lambda: "Lambda", openApiSchema: "OpenAPI", smithyModel: "Smithy", apiGateway: "API Gateway" };
  return labels[kind] ?? kind;
}

export const listUnmanagedTargets = () => getJson<{ targets: UnmanagedTarget[] }>("/api/gateway-tools/unmanaged");

/** Manage a hand-made Lambda target: its inline schema becomes the approved baseline. */
export const adoptUnmanagedTarget = (targetId: string) =>
  sendJson<LambdaToolTarget>(`/api/gateway-tools/unmanaged/${enc(targetId)}/adopt`, "POST");

/** The Lambda-ARN placeholder for the current workspace — the function must live in its
 *  account and region (the backend refuses any other), so the example names them. */
export function lambdaArnPlaceholder(ws: { account_id?: string | null; region?: string | null } | null): string {
  const region = ws?.region || "<region>";
  const partition = region.startsWith("cn-") ? "aws-cn" : region.startsWith("us-gov-") ? "aws-us-gov" : "aws";
  return `arn:${partition}:lambda:${region}:${ws?.account_id || "<account-id>"}:function:crm-tools`;
}

/** The review calls of one subject: a hosted server, or a Lambda / adopted target. */
export interface ReviewApi {
  list: () => Promise<McpReviewList>;
  get: (reviewId: string) => Promise<McpReviewDetail>;
  approve: (reviewId: string, note?: string) => Promise<McpReviewDetail>;
  reject: (reviewId: string, note?: string) => Promise<McpReviewDetail>;
  /** hosted servers only: re-sync and compare (drift) */
  check?: () => Promise<McpDriftCheck>;
}

export function serverReviewApi(agentId: string): ReviewApi {
  return {
    list: () => listMcpReviews(agentId),
    get: (id) => getMcpReview(agentId, id),
    approve: (id, note = "") => approveMcpReview(agentId, id, note),
    reject: (id, note = "") => rejectMcpReview(agentId, id, note),
    check: () => checkMcpDrift(agentId),
  };
}

/** `/api/gateway-tools/{toolId}/reviews[/{...rest}]`, each segment encoded. */
export function toolReviewPath(toolId: string, ...rest: string[]): string {
  return [`/api/gateway-tools/${enc(toolId)}/reviews`, ...rest.map(enc)].join("/");
}

export function toolReviewApi(toolId: string): ReviewApi {
  return {
    list: () => getJson<McpReviewList>(toolReviewPath(toolId)),
    get: (id) => getJson<McpReviewDetail>(toolReviewPath(toolId, id)),
    approve: (id, note = "") => sendJson<McpReviewDetail>(toolReviewPath(toolId, id, "approve"), "POST", { note }),
    reject: (id, note = "") => sendJson<McpReviewDetail>(toolReviewPath(toolId, id, "reject"), "POST", { note }),
  };
}

/** Parse the tool-schema textarea: a JSON list of tool definitions (or `{tools: [...]}`). */
export function parseToolSchema(text: string): { tools: LambdaToolDefinition[] } | { error: "json" | "shape" } {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return { error: "json" };
  }
  const list = Array.isArray(raw) ? raw : raw && typeof raw === "object" && Array.isArray((raw as { tools?: unknown }).tools) ? (raw as { tools: unknown[] }).tools : null;
  if (!list || list.length === 0) return { error: "shape" };
  const ok = list.every(
    (t) =>
      t &&
      typeof t === "object" &&
      typeof (t as LambdaToolDefinition).name === "string" &&
      (t as LambdaToolDefinition).inputSchema !== null &&
      typeof (t as LambdaToolDefinition).inputSchema === "object",
  );
  return ok ? { tools: list as LambdaToolDefinition[] } : { error: "shape" };
}

/** Same rule as the backend's `MCP_TARGET_NAME_RE` (letters / digits, single dashes). */
export const TARGET_NAME_RE = /^[a-zA-Z0-9](?:-?[a-zA-Z0-9]){0,99}$/;
export const LAMBDA_ARN_RE = /^arn:aws[a-z-]*:lambda:[a-z0-9-]+:\d{12}:function:[A-Za-z0-9_-]{1,64}(?::[A-Za-z0-9_$-]{1,128})?$/;

export interface LambdaFormErrors {
  arn?: string;
  name?: string;
  schema?: string;
}

/** Client-side checks before the request (the server checks again, and more). */
export function validateLambdaForm(arn: string, name: string, schema: string): LambdaFormErrors {
  const errors: LambdaFormErrors = {};
  if (!LAMBDA_ARN_RE.test(arn.trim())) errors.arn = "lambdaTool.form.badArn";
  if (!TARGET_NAME_RE.test(name.trim())) errors.name = "lambdaTool.form.badName";
  const parsed = parseToolSchema(schema);
  if ("error" in parsed) errors.schema = parsed.error === "json" ? "lambdaTool.form.badJson" : "lambdaTool.form.badShape";
  return errors;
}

/**
 * The tool targets Gateway governance raises in 需要处理 (both consoles): Lambda
 * targets whose publish failed, and targets — Lambda or adopted external — whose
 * schema waits for a reviewer. Names only; each console owns the copy.
 */
export function targetsNeedingAttention(
  lambdas: LambdaToolTarget[],
  external: ExternalMcpTarget[],
): { failed: string[]; review: string[] } {
  return {
    failed: lambdas.filter((x) => x.status === "failed").map((x) => x.target_name),
    review: [
      ...lambdas.filter((x) => x.review.state === "reviewing").map((x) => x.target_name),
      ...external.filter((x) => x.review?.state === "reviewing").map((x) => x.target_name),
    ],
  };
}

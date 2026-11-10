// Pure governance helpers shared by the classic `/governance` page and the V2
// console: status classification, gateway predicates and the Cedar draft templates
// of the self-hosted Policy module (D20). No React, no components.
import {
  ApiError,
  type GovernanceGatewaySummary,
  type PolicyDecisionOutcome,
  type PolicyEntityTool,
  type PolicyStatus,
  type PolicyValidationReport,
} from "./api";
import i18n from "../i18n";

export type GovernanceStatusLevel = "good" | "crit" | "warn" | "muted";

const GOOD = ["READY", "ACTIVE", "SUCCEEDED", "APPROVED", "ALLOW", "PASS"];
const CRIT = [
  "FAILED",
  "ERROR",
  "DENY",
  "REJECTED",
  "PARTIAL",
  "SYNCHRONIZE_UNSUCCESSFUL",
  "UPDATE_UNSUCCESSFUL",
];
const WARN = [
  "CREATING",
  "UPDATING",
  "PENDING",
  "RUNNING",
  "SUBMITTED",
  "SYNCHRONIZING",
  "CREATE_PENDING_AUTH",
  "UPDATE_PENDING_AUTH",
  "SYNCHRONIZE_PENDING_AUTH",
];

/** Severity of an AWS / governance status string (gateway, engine, policy, operation, decision). */
export function governanceStatusLevel(status: string | null | undefined): GovernanceStatusLevel {
  const normalized = status?.toUpperCase() ?? "";
  if (GOOD.includes(normalized)) return "good";
  if (CRIT.includes(normalized)) return "crit";
  if (WARN.includes(normalized)) return "warn";
  return "muted";
}

export function governanceError(error: unknown): string {
  if (error instanceof ApiError) {
    const detail = error.detail;
    if (detail && typeof detail === "object" && "source" in detail
        && detail.source === "tools_list") {
      const status = "upstream_status" in detail ? detail.upstream_status : null;
      const attempts = "attempts" in detail ? detail.attempts : null;
      if (typeof status === "number" && Number.isInteger(status) && status >= 400 && status <= 599
          && typeof attempts === "number" && Number.isInteger(attempts)
          && attempts >= 1 && attempts <= 3) {
        return `${error.code}: ${error.message} ${i18n.t("apiErrors.policy.schema_http_failure", { status, attempts })}`;
      }
    }
    return `${error.code}: ${error.message}`;
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

/** Both draft validation and install failures carry compiler diagnostics in detail.errors. */
export function policyValidationError(error: unknown): PolicyValidationReport | null {
  if (!(error instanceof ApiError) || !error.code.startsWith("policy.")) return null;
  if (!error.detail || typeof error.detail !== "object" || !("errors" in error.detail)) return null;
  const errors = error.detail.errors;
  if (!Array.isArray(errors) || !errors.length) return null;
  return {
    ok: false,
    errors: errors.map((issue: unknown) => {
      if (typeof issue === "object" && issue !== null) {
        const value = issue as Record<string, unknown>;
        return {
          msg: String(value.message ?? value.msg ?? JSON.stringify(issue)),
          line: typeof value.line === "number" ? value.line : null,
          col: typeof value.col === "number" ? value.col : null,
        };
      }
      return { msg: String(issue), line: null, col: null };
    }),
    warnings: [],
  };
}

export function isGatewayReady(gateway: GovernanceGatewaySummary): boolean {
  return gateway.status.toUpperCase() === "READY";
}

// SynchronizeGatewayTargets: initialize + paginated tools/list against the MCP
// server; poll the detail every few seconds, give up after ~2 minutes.
export const TARGET_SYNC_POLL_INTERVAL_MS = 4000;
export const TARGET_SYNC_POLL_MS = 120_000;

/* ── Policy (D20): names, tones and the `Launchpad::` Cedar templates ──────── */

/** Policy names the store accepts (Global `POLICY_NAME_PATTERN`): a letter, then up to 47
 *  letters / digits / underscores. */
export const POLICY_NAME_RE = /^[A-Za-z][A-Za-z0-9_]{0,47}$/;

/** The Cedar namespace of the self-hosted schema (`backend/app/policy/schema.cedarschema.json`). */
export const CEDAR_NAMESPACE = "Launchpad";

/** Attachment mode → severity: `ENFORCE` denies, `LOG_ONLY` observes. Historic decision
 *  rows still carry the retired workspace modes (`enforce` / `log_only` / `off`) — same reading. */
export function policyModeLevel(mode: string | null | undefined): GovernanceStatusLevel {
  const m = mode?.toLowerCase();
  if (m === "enforce") return "good";
  if (m === "log_only") return "warn";
  return "muted";
}

/** Row status → severity (draft is "not live yet", retired is inert). */
export function policyStatusLevel(status: PolicyStatus | string | null | undefined): GovernanceStatusLevel {
  if (status === "active") return "good";
  if (status === "draft") return "warn";
  return "muted";
}

/** Decision outcome → severity. `would_deny` is the LOG_ONLY evidence an operator promotes on. */
export function policyDecisionLevel(decision: PolicyDecisionOutcome | string | null | undefined): GovernanceStatusLevel {
  if (decision === "allow") return "good";
  if (decision === "deny" || decision === "error") return "crit";
  if (decision === "would_deny") return "warn";
  return "muted";
}

function cedarString(value: string): string {
  return value.replaceAll("\\", "\\\\").replaceAll('"', '\\"');
}

const entity = (type: string, id: string) => `${CEDAR_NAMESPACE}::${type}::"${cedarString(id)}"`;
const ACTION_CALL_TOOL = `${CEDAR_NAMESPACE}::Action::"call_tool"`;

/** The editor's prefill templates — mirrors `backend/app/policy/templates/` (§2.1). Policies
 *  decide TOOL CALLS only (Global parity, contract amendment 9): the two baselines are the
 *  Stage-A workspace permit (every agent of the workspace may call its declared tools) and the
 *  Stage-BT users permit (members, administrators, system principals); there is no invoke
 *  template any more. */
export type PolicyPrefill =
  | "permit_workspace_agents_call_tools"
  | "permit_users_call_tools"
  | "permit_agent_declared_tools"
  | "forbid_tool_for_members"
  | "custom";
export const POLICY_PREFILLS: PolicyPrefill[] = [
  "permit_workspace_agents_call_tools",
  "permit_users_call_tools",
  "permit_agent_declared_tools",
  "forbid_tool_for_members",
  "custom",
];
/** The placeholder the Stage-A workspace template carries; the console substitutes it. */
export const WORKSPACE_ID_PLACEHOLDER = "__WORKSPACE_ID__";

/** A server template row (`GET /api/policies/entities` → `templates[]`, `backend/app/policy/templates/*.cedar`). */
export interface ServerTemplate {
  name: string;
  statement: string;
}

/** The server template's statement for `name` when the entities payload carries it, else null. */
export function serverTemplate(
  entities: { templates?: ServerTemplate[] | null } | null | undefined,
  name: string,
): string | null {
  return entities?.templates?.find((row) => row.name === name)?.statement ?? null;
}

/** Canonical form of a Cedar template: `//` comment lines dropped, outer whitespace trimmed —
 *  what the editor pastes and what `backend/tests/test_policy_templates.py` pins. */
export function canonicalCedar(statement: string): string {
  return statement
    .split("\n")
    .filter((line) => !line.trim().startsWith("//"))
    .join("\n")
    .trim();
}

function permitCallTool(principal: string): string {
  return `permit(
  ${principal},
  action == ${ACTION_CALL_TOOL},
  resource
);`;
}

/** Baseline, Stage A (the agent): every agent of this workspace may call the Gateway tools it
 *  declares — the first policy to publish once the interceptor is attached, because Cedar's
 *  default deny otherwise refuses every call (amendment 9 "default-deny baseline"). Emits the
 *  canonical form of the server template `permit_workspace_agents_call_tools.cedar` with
 *  `__WORKSPACE_ID__` substituted (pinned by `governance-prefill.test.ts` against the file and
 *  by `test_policy_templates.py`); when the entities payload carries the server template, that
 *  one is used (canonical form, placeholder substituted). */
export function buildPermitWorkspaceAgentsCallTools(workspaceId: string, fromServer?: string | null): string {
  const base = fromServer && fromServer.trim()
    ? canonicalCedar(fromServer)
    : permitCallTool(`principal in ${entity("Workspace", WORKSPACE_ID_PLACEHOLDER)}`);
  return base.replaceAll(WORKSPACE_ID_PLACEHOLDER, cedarString(workspaceId));
}

/** Baseline, Stage BT (the user behind the call): members, administrators, the system
 *  principals (eval / canary tokens) and `/v1` API keys (lead ruling option D, Global M2M parity)
 *  may call every tool; narrow it with a forbid or a tool-specific permit instead of removing
 *  it. D20-obs2 still holds: a user has exactly ONE `Role` parent, so a member-only permit would
 *  default-deny administrators. Emits the canonical form of `permit_users_call_tools.cedar`
 *  (four permits); the server template is used verbatim when present. */
export function buildPermitUsersCallTools(fromServer?: string | null): string {
  if (fromServer && fromServer.trim()) return canonicalCedar(fromServer);
  return [
    permitCallTool(`principal in ${entity("Role", "member")}`),
    permitCallTool(`principal in ${entity("Role", "admin")}`),
    permitCallTool(`principal is ${CEDAR_NAMESPACE}::System`),
    permitCallTool(`principal is ${CEDAR_NAMESPACE}::ApiKey`),
  ].join("\n\n");
}

/** A tool row the prefill builders accept: the catalog entry (`PolicyEntityTool`, whose
 *  `pattern` flag the server derives) or a bare id. A name is a pattern when the server says
 *  so or, failing that, when it carries an fnmatch metacharacter (`*`, `?`, `[`) — the same
 *  rule `GET /api/policies/entities` applies. */
export type PrefillTool = string | (Pick<PolicyEntityTool, "name"> & { pattern?: boolean | null });

/** fnmatch metacharacters — a concrete `<target>___<tool>` id never carries one. */
export function isToolPattern(name: string): boolean {
  return /[*?[]/.test(name);
}

function prefillTool(tool: PrefillTool): { name: string; pattern: boolean } {
  if (typeof tool === "string") return { name: tool, pattern: isToolPattern(tool) };
  return { name: tool.name, pattern: tool.pattern ?? isToolPattern(tool.name) };
}

/** The `when` clause for a mix of pattern and concrete rows: one `resource.name like`
 *  per pattern (verbatim, mirrors `templates/permit_agent_declared_tools.cedar`) and one
 *  entity literal / set for the concrete ids. Cedar validates `resource.name` because
 *  `call_tool` applies to `Tool` only. */
function toolResourceCondition(tools: PrefillTool[]): { scope: string; when: string | null } {
  const rows = tools.map(prefillTool);
  const literals = rows.filter((r) => !r.pattern).map((r) => r.name);
  const patterns = rows.filter((r) => r.pattern).map((r) => r.name);
  const literalClause =
    literals.length === 0
      ? null
      : literals.length === 1
        ? `resource == ${entity("Tool", literals[0])}`
        : `resource in [${literals.map((tool) => entity("Tool", tool)).join(", ")}]`;
  if (patterns.length === 0) {
    // Cedar's scope `resource in <x>` takes ONE entity (a group), never a set: a plural
    // allowlist must live in `when { resource in [...] }` (rev-r8 on p1-c3).
    if (literals.length <= 1) return { scope: literalClause ?? "resource", when: null };
    return { scope: "resource", when: literalClause };
  }
  const clauses = [...patterns.map((p) => `resource.name like "${cedarString(p)}"`), ...(literalClause ? [literalClause] : [])];
  return { scope: "resource", when: clauses.join(" ||\n  ") };
}

/** `permit` for exactly the listed Gateway tools (an action allowlist, not a user allowlist).
 *  `agentId` narrows the principal to one agent (the deploy-time compilation, §2.3(b));
 *  without it every principal may call the tools. A pattern row (`crm___*`) becomes
 *  `resource.name like "crm___*"`; a concrete row stays an entity literal. */
export function buildAllowlistStatement(tools: PrefillTool[], agentId: string | null = null): string {
  const { scope, when } = toolResourceCondition(tools);
  return `permit(
  principal${agentId ? ` == ${entity("Agent", agentId)}` : ""},
  action == ${ACTION_CALL_TOOL},
  ${scope}
)${when ? ` when {
  ${when}
}` : ""};`;
}

/** `forbid` one tool (or one pattern) for everyone but admins (the `payout_admin_only`
 *  analogue, §2.1). */
export function buildForbidToolForMembers(tool: PrefillTool): string {
  const { scope, when } = toolResourceCondition([tool]);
  return `forbid(
  principal,
  action == ${ACTION_CALL_TOOL},
  ${scope}
)${when ? ` when {
  ${when}
}` : ""} unless { principal in ${entity("Role", "admin")} };`;
}

/** A concrete tool id that matches an fnmatch pattern — what the simulator sends for a
 *  pattern row, since the engine decides `Tool::<id>` entities, never globs:
 *  `crm___*` → `crm___example`, `a?` → `ax`, `[!x]` / `[abc]` → `y` / `a`. */
export function patternExampleName(pattern: string): string {
  return pattern
    .replace(/\[!([^\]])[^\]]*\]/g, (_m, first: string) => (first === "y" ? "z" : "y"))
    .replace(/\[([^\]])[^\]]*\]/g, "$1")
    .replaceAll("*", "example")
    .replaceAll("?", "x");
}

/** Broad baseline `permit` for every tool call (avoids default-deny disruption). */
export function buildPreserveTrafficStatement(): string {
  return `permit(
  principal,
  action == ${ACTION_CALL_TOOL},
  resource
);`;
}

/**
 * A tool-level starting point for one gateway target: its tools from the entity catalog
 * and a policy name the store accepts. A target with no catalogued tools yields no
 * tools — the editor then leaves the allowlist empty.
 */
export function toolPolicyPrefill(
  tools: Pick<PolicyEntityTool, "name" | "target">[],
  targetName: string,
): { name: string; tools: string[] } {
  const slug = targetName.replace(/[^A-Za-z0-9_]/g, "_");
  const name = `allow_${slug}`.slice(0, 48);
  return {
    name: POLICY_NAME_RE.test(name) ? name : "launchpad_policy",
    tools: tools.filter((tool) => tool.target === targetName).map((tool) => tool.name),
  };
}

/** A JSON object from the simulator's context textarea, or null when it is not one. */
export function parseArgumentsObject(text: string): Record<string, unknown> | null {
  let value: unknown;
  try {
    value = JSON.parse(text.trim() || "{}");
  } catch {
    return null;
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

// Pure governance helpers shared by the classic `/governance` page and the V2
// console: status classification, operation/gateway predicates and the Cedar
// draft templates. No React, no components.
import {
  ApiError,
  type GovernanceGatewayAction,
  type GovernanceGatewayDetail,
  type GovernanceGatewaySummary,
  type GovernanceOperation,
  type GovernancePolicyTestIdentity,
} from "./api";

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
  "LOG_ONLY",
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
  if (error instanceof ApiError) return `${error.code}: ${error.message}`;
  if (error instanceof Error) return error.message;
  return String(error);
}

export function isGatewayReady(gateway: GovernanceGatewaySummary): boolean {
  return gateway.status.toUpperCase() === "READY";
}

export function isOperationPending(operation: GovernanceOperation | null): boolean {
  return operation?.status === "pending" || operation?.status === "running";
}

/** Poll cadence of a pending governance operation. */
export const GOVERNANCE_OPERATION_POLL_MS = 2500;

// SynchronizeGatewayTargets: initialize + paginated tools/list against the MCP
// server; poll the detail every few seconds, give up after ~2 minutes.
export const TARGET_SYNC_POLL_INTERVAL_MS = 4000;
export const TARGET_SYNC_POLL_MS = 120_000;

/** Policy names AWS accepts: a letter, then up to 47 letters / digits / underscores. */
export const POLICY_NAME_RE = /^[A-Za-z][A-Za-z0-9_]{0,47}$/;

function cedarString(value: string): string {
  return value.replaceAll("\\", "\\\\").replaceAll('"', '\\"');
}

function cedarPrincipal(gateway: GovernanceGatewayDetail): string {
  return gateway.authorizer_type === "AWS_IAM" ? "AgentCore::IamEntity" : "AgentCore::OAuthUser";
}

/** `permit` for exactly the listed Gateway actions (an action allowlist, not a user allowlist). */
export function buildAllowlistStatement(gateway: GovernanceGatewayDetail, actions: string[]): string {
  const actionClause =
    actions.length === 1
      ? `action == AgentCore::Action::"${cedarString(actions[0])}"`
      : `action in [${actions.map((action) => `AgentCore::Action::"${cedarString(action)}"`).join(", ")}]`;
  return `permit(
  principal is ${cedarPrincipal(gateway)},
  ${actionClause},
  resource == AgentCore::Gateway::"${cedarString(gateway.arn)}"
);`;
}

/** Broad baseline `permit` for every action on the Gateway (avoids default-deny disruption). */
export function buildPreserveTrafficStatement(gateway: GovernanceGatewayDetail): string {
  return `permit(
  principal is ${cedarPrincipal(gateway)},
  action,
  resource == AgentCore::Gateway::"${cedarString(gateway.arn)}"
);`;
}

/** The two demo identities the policy-test endpoint signs in as. */
export const POLICY_TEST_IDENTITIES: { value: GovernancePolicyTestIdentity; label: string }[] = [
  { value: "demo", label: "demo@hr-analyst" },
  { value: "admin", label: "admin@platform-admin" },
];

/** The demo payout tool when present, else the first verified action, else the first. */
export function preferredTestAction(actions: GovernanceGatewayAction[]): string {
  return (
    actions.find((action) => action.name === "hr-database___create_payout")?.name ??
    actions.find((action) => action.verified)?.name ??
    actions[0]?.name ??
    ""
  );
}

/** Verified actions first, then by name. */
export function sortTestActions(actions: GovernanceGatewayAction[]): GovernanceGatewayAction[] {
  return [...actions].sort(
    (left, right) => Number(right.verified) - Number(left.verified) || left.name.localeCompare(right.name),
  );
}

/** Required input fields declared by an action's JSON schema. */
export function requiredSchemaFields(action: GovernanceGatewayAction | undefined): string[] {
  const required = action?.input_schema?.required;
  return Array.isArray(required) ? required.filter((field): field is string => typeof field === "string") : [];
}

/** A JSON object from the arguments textarea, or null when it is not one. */
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

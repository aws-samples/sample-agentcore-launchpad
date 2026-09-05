import type { ChipTone } from "../../components";
import {
  ApiError,
  type GovernanceGatewaySummary,
  type GovernanceOperation,
} from "../../lib/api";

export type GovernanceView =
  | "gateways"
  | "gateway"
  | "policy"
  | "decisions"
  | "audit"
  | "tools";

export const GOVERNANCE_VIEWS = new Set<GovernanceView>([
  "gateways",
  "gateway",
  "policy",
  "decisions",
  "audit",
  "tools",
]);

export function governanceViewFromParam(value: string | null): GovernanceView {
  if (value && GOVERNANCE_VIEWS.has(value as GovernanceView)) {
    return value as GovernanceView;
  }
  return "gateways";
}

export function governanceError(error: unknown): string {
  if (error instanceof ApiError) return `${error.code}: ${error.message}`;
  if (error instanceof Error) return error.message;
  return String(error);
}

export function statusTone(status: string | null | undefined): ChipTone {
  const normalized = status?.toUpperCase() ?? "";
  if (["READY", "ACTIVE", "SUCCEEDED", "APPROVED", "ALLOW", "PASS"].includes(normalized)) {
    return "good";
  }
  if (
    [
      "FAILED",
      "ERROR",
      "DENY",
      "REJECTED",
      "PARTIAL",
      "SYNCHRONIZE_UNSUCCESSFUL",
      "UPDATE_UNSUCCESSFUL",
    ].includes(normalized)
  ) {
    return "crit";
  }
  if (
    [
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
    ].includes(normalized)
  ) {
    return "warn";
  }
  return "muted";
}

export function isGatewayReady(gateway: GovernanceGatewaySummary): boolean {
  return gateway.status.toUpperCase() === "READY";
}

export function isOperationPending(operation: GovernanceOperation | null): boolean {
  return operation?.status === "pending" || operation?.status === "running";
}

export function formatTimestamp(value: string | null | undefined, locale: string): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(locale);
}

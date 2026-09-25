import type { ChipTone } from "../../components";
import { governanceStatusLevel } from "../../lib/governance";

// The pure helpers live in `lib/governance.ts` so the V2 console shares them.
export { governanceError, isGatewayReady, isOperationPending } from "../../lib/governance";

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

export function statusTone(status: string | null | undefined): ChipTone {
  return governanceStatusLevel(status);
}

export function formatTimestamp(value: string | null | undefined, locale: string): string {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString(locale);
}

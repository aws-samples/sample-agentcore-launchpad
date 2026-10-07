// Governance policy-deny card (V2 Chat): a Gateway tool call a Cedar policy denied.
import type { ChatHistoryMessage, ChatStreamPayload } from "./api";

/** One denied call, from the live `policy_denied` event or a restored `policy` row. */
export interface PolicyDeny {
  /** catalog tool name, e.g. `hr-database___create_payout` */
  tool: string;
  /** the Gateway's bracketed reason */
  reason: string;
  policyId: string | null;
  /** the denying gateway when the call went through the workspace gateway; a
   *  restored row has none (history keeps tool + reason only) */
  gatewayId: string | null;
}

// mirrors backend app/services/policy_denials.DETERMINING_POLICY_RE
const DETERMINING_POLICY_RE = /Policy evaluation denied due to ([A-Za-z0-9][A-Za-z0-9_-]*)/i;

export function policyIdFrom(reason: string): string | null {
  return DETERMINING_POLICY_RE.exec(reason)?.[1] ?? null;
}

export function livePolicyDeny(payload: Partial<ChatStreamPayload>): PolicyDeny {
  const reason = payload.reason ?? "";
  return {
    tool: payload.tool ?? "",
    reason,
    policyId: payload.policy_id ?? policyIdFrom(reason),
    gatewayId: payload.gateway_id || null,
  };
}

export function restoredPolicyDeny(row: Pick<ChatHistoryMessage, "text" | "name">): PolicyDeny {
  return { tool: row.name ?? "", reason: row.text, policyId: policyIdFrom(row.text), gatewayId: null };
}

/** The gateway's policies when known, else the Governance landing page. */
export function policyDenyHref(deny: PolicyDeny): string {
  if (!deny.gatewayId) return "/v2/governance";
  const params = new URLSearchParams({ view: "gateway", gateway: deny.gatewayId, section: "policies" });
  return `/v2/governance?${params.toString()}`;
}

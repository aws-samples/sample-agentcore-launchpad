import type { GovernanceGatewaySummary } from "../../../lib/api";
import type { TagTone } from "../../ui";
import { policyDecisionLevel, policyModeLevel, policyStatusLevel } from "../../../lib/governance";

/**
 * The Policy module (Global AgentCore Policy structure): `/v2/policy` is the engine list;
 * `?view=engine&engine=<id>&tab=` is one engine with its four tabs; `?view=policy&engine=<id>
 * [&policy=<pid>]` the editor; `?policy=<pid>[&tab=decisions]` a resolver for links that know a
 * policy id but not its engine (the chat deny card, decision rows).
 */
const LEVEL_TONE = { good: "green", crit: "red", warn: "orange", muted: "gray" } as const;

export const policyModeTone = (mode: string | null | undefined): TagTone => LEVEL_TONE[policyModeLevel(mode)];
export const policyStatusTone = (status: string | null | undefined): TagTone => LEVEL_TONE[policyStatusLevel(status)];
export const policyDecisionTone = (decision: string | null | undefined): TagTone =>
  LEVEL_TONE[policyDecisionLevel(decision)];

/** Tabs of one engine's detail page. */
export type EngineTab = "policies" | "simulate" | "decisions" | "audit" | "gateways";
export const ENGINE_TABS: EngineTab[] = ["policies", "simulate", "decisions", "audit", "gateways"];
export const isEngineTab = (value: string | null): value is EngineTab =>
  !!value && (ENGINE_TABS as string[]).includes(value);

/** Global's engine name rule: a letter, then up to 47 letters / digits / underscores. */
export const ENGINE_NAME_RE = /^[A-Za-z][A-Za-z0-9_]{0,47}$/;

export const POLICY_PATH = "/v2/policy";

export function engineHref(engineId: string, tab: EngineTab = "policies"): string {
  const params = new URLSearchParams({ view: "engine", engine: engineId });
  if (tab !== "policies") params.set("tab", tab);
  return `${POLICY_PATH}?${params.toString()}`;
}

export function policyEditorHref(engineId: string, policyId: string | null = null): string {
  const params = new URLSearchParams({ view: "policy", engine: engineId });
  if (policyId) params.set("policy", policyId);
  return `${POLICY_PATH}?${params.toString()}`;
}

/** The V2 agent detail page (decision rows link the calling agent). */
export function agentDetailHref(agentId: string): string {
  return `/v2/agents?view=detail&agent=${encodeURIComponent(agentId)}`;
}

/** The Gateway detail's 策略引擎 section. */
export function gatewayPolicyHref(gatewayId: string): string {
  return `/v2/governance?view=gateway&gateway=${encodeURIComponent(gatewayId)}&section=policy-engine`;
}

/** The region of a gateway ARN (`arn:aws-cn:bedrock-agentcore:cn-north-1:…`), else null. */
export function gatewayRegion(gateway: Pick<GovernanceGatewaySummary, "arn">): string | null {
  const parts = gateway.arn.split(":");
  return parts.length > 4 && parts[3] ? parts[3] : null;
}

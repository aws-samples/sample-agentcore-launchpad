// Governance policy-deny card (V2 Chat, D20; per-user policy A6; Global parity, contract
// amendment 9): a GATEWAY TOOL CALL Launchpad's policy refused — decided by the Gateway
// interceptor for the user the agent acts for (SSE `policy_denied`). Policies decide tool
// calls only: there is no invoke denial any more (the hub's invoke gate was retired), so the
// only kind is `tool`. The card names the denying POLICY and the engine STAGE (阶段 A /
// 阶段 BT) in the console's language; the raw English engine text stays available under a
// details toggle. Historic `hub` / `runtime` Decisions rows still render through the same card.
import type { ChatHistoryMessage, ChatStreamPayload, PolicyDecision, PolicyDenyStage } from "./api";

export type PolicyDenyKind = "tool";
/** Who decided: the Gateway interceptor or a recorded simulator dry run (Decisions detail
 *  only); `hub` (the retired invoke gate) and `runtime` (the retired runtime guard) name the
 *  source of a HISTORIC Decisions row. */
export type PolicyDenySource = "hub" | "runtime" | "gateway" | "simulate";

/** One denied tool call, from the live event, a restored `policy` row or a Decisions row. */
export interface PolicyDeny {
  kind: PolicyDenyKind;
  /** catalog tool name, e.g. `hr-database___create_payout`; "" when the row named none */
  tool: string;
  /** the reason text (the raw English engine text) */
  reason: string;
  /** determining policy id (`pol_…`), when known */
  policyId: string | null;
  /** the engine that owns the policy (the engine attached to the deciding gateway), when the
   *  frame / row named it — the links go straight to it; else the Policy page resolves it */
  engineId?: string | null;
  /** audit row id — the Decisions tab can be filtered on it */
  decisionId: string | null;
  /** Decisions rows only: the agent the row named */
  agentId?: string | null;
  /** the attachment mode the decision was made under, when the row kept it */
  mode?: string | null;
  /** A6: the denying policy's name, when the frame / row named it */
  policyName?: string | null;
  /** N17-obs507: every determining policy the Gateway named (ids) and their display names
   *  (name, else id), in the Gateway's order — the headline lists them all */
  policyIds?: string[];
  policyNames?: string[];
  /** N17-obs509: whose call was decided — the Decisions ROW's principal (label / id + kind).
   *  Absent on a live / restored chat frame: the turn's user is the viewer, read from auth. */
  principal?: { name: string; role?: string | null };
  /** A6: the engine stage that denied — `a` / `bt` / `final` */
  stage?: PolicyDenyStage | null;
  /** A6: who decided */
  source?: PolicyDenySource | null;
  /** a `would_deny` observation (mode log_only — the call ran) */
  observed?: boolean;
}

// mirrors backend app/services/policy_denials.DETERMINING_POLICY_RE (Global signature kept
// so a runtime-guard denial text is recognised unchanged)
const DETERMINING_POLICY_RE = /Policy evaluation denied due to ([A-Za-z0-9][A-Za-z0-9_:-]*)/i;

const STAGES: readonly string[] = ["a", "bt", "final"];
const SOURCES: readonly string[] = ["hub", "runtime", "gateway", "simulate"];

/** A stage the card can name, else null (an unknown value is never shown). */
export function stageOf(value: unknown): PolicyDenyStage | null {
  return typeof value === "string" && STAGES.includes(value) ? (value as PolicyDenyStage) : null;
}

function sourceOf(value: unknown): PolicyDenySource | null {
  return typeof value === "string" && SOURCES.includes(value) ? (value as PolicyDenySource) : null;
}

const text = (value: unknown): string | null => (typeof value === "string" && value ? value : null);

/** `pol_ab12:v3` (the `@id` annotation, §4.2) → `pol_ab12`. */
export function policyIdFromDetermining(id: string | null | undefined): string | null {
  if (!id) return null;
  const bare = id.split(":")[0];
  return bare || null;
}

export function policyIdFrom(reason: string): string | null {
  return policyIdFromDetermining(DETERMINING_POLICY_RE.exec(reason)?.[1] ?? null);
}

/** The optional A6 facts, only the ones present (a card without them renders as before). */
function facts(input: { policyName?: unknown; stage?: unknown; source?: unknown; engineId?: unknown }): Partial<PolicyDeny> {
  const out: Partial<PolicyDeny> = {};
  const engineId = text(input.engineId);
  if (engineId) out.engineId = engineId;
  const name = text(input.policyName);
  const stage = stageOf(input.stage);
  const source = sourceOf(input.source);
  if (name) out.policyName = name;
  if (stage) out.stage = stage;
  if (source) out.source = source;
  return out;
}

/** The live `policy_denied` SSE event: a Gateway interceptor denial re-emitted by the hub
 *  (`source: "gateway"`, A6). */
export function livePolicyDeny(payload: Partial<ChatStreamPayload>): PolicyDeny {
  const reason = payload.reason ?? "";
  const ids = Array.isArray(payload.policy_ids) ? payload.policy_ids.filter((x): x is string => typeof x === "string" && !!x) : [];
  const names = (payload.policy_names ?? {}) as Record<string, unknown>;
  return {
    kind: "tool",
    tool: payload.tool ?? "",
    reason,
    policyId: policyIdFromDetermining(payload.policy_id) ?? policyIdFromDetermining(ids[0]) ?? policyIdFrom(reason),
    decisionId: payload.decision_id ?? null,
    ...facts({ policyName: payload.policy_name, stage: payload.stage, source: payload.source, engineId: payload.engine_id }),
    // N17-obs507: the Gateway names every determining policy; the card lists them all
    ...(ids.length > 0
      ? { policyIds: ids, policyNames: ids.map((id) => (typeof names[id] === "string" && names[id] ? (names[id] as string) : id)) }
      : {}),
  };
}

/** What the headline calls the policy: every name the Gateway listed (N17-obs507), else the
 *  single name, else the id. */
export function policyLabel(deny: PolicyDeny): string | null {
  if (deny.policyNames && deny.policyNames.length > 0) return deny.policyNames.join("、");
  return deny.policyName ?? deny.policyId ?? null;
}

/** A restored history row (`role: "policy"`, text = reason, name = tool — null on a row an
 *  older build wrote for the retired invoke gate; A6: the live frame's facts under `policy`). */
export function restoredPolicyDeny(row: Pick<ChatHistoryMessage, "text" | "name" | "policy">): PolicyDeny {
  const tool = row.name ?? "";
  const kept = row.policy ?? {};
  return {
    kind: "tool",
    tool,
    reason: row.text,
    policyId: policyIdFromDetermining(kept.policy_id) ?? policyIdFrom(row.text),
    decisionId: text(kept.decision_id),
    ...(text(kept.mode) ? { mode: kept.mode } : {}),
    ...facts({ policyName: kept.policy_name, stage: kept.stage, source: kept.source, engineId: kept.engine_id }),
  };
}

/** A Decisions row (`GET /api/policy-engines/{id}/decisions`) as the card's input — the Decisions detail
 *  renders the same card as Chat. `names` maps policy ids to names (the workspace's list). */
export function decisionPolicyDeny(row: PolicyDecision, names: Record<string, string> = {}): PolicyDeny {
  const reason = row.reason ?? "";
  // N17-obs509: every determining policy of the row (`pol_x:v2` → `pol_x`), named from the list
  const ids = row.policy_ids.map((id) => policyIdFromDetermining(id)).filter((id): id is string => !!id);
  const policyId = ids[0] ?? policyIdFrom(reason);
  // a historic hub row (the retired invoke gate) named an agent, not a tool
  const tool = row.resource_kind === "tool" ? row.resource_id ?? "" : "";
  return {
    kind: "tool",
    tool,
    reason,
    policyId,
    decisionId: row.id,
    ...(row.agent_id ? { agentId: row.agent_id } : {}),
    mode: row.mode,
    // N17-obs509: the ROW's principal, never the viewer
    principal: { name: row.principal_label ?? row.principal_id ?? "—", role: row.principal_kind ?? null },
    ...facts({
      policyName: policyId ? names[policyId] : null,
      // a gateway row names its engine stage; a historic hub row has none
      stage: row.stage ?? null,
      source: row.source,
      engineId: row.engine_id,
    }),
    ...(ids.length > 0 ? { policyIds: ids, policyNames: ids.map((id) => names[id] ?? id) } : {}),
    // N17-obs508: a gateway ALLOW a log_only policy would have denied is an observation too
    ...(row.decision === "would_deny" || row.log_only_would ? { observed: true } : {}),
  };
}

/** The policy's editor: direct when the frame / row named the engine, else through the Policy
 *  page's resolver (`?policy=`); without a policy id the Policy module (engine list). */
export function policyDenyHref(deny: PolicyDeny): string {
  if (!deny.policyId) return deny.engineId ? `/v2/policy?view=engine&engine=${encodeURIComponent(deny.engineId)}` : "/v2/policy";
  if (deny.engineId) return `/v2/policy?view=policy&engine=${encodeURIComponent(deny.engineId)}&policy=${encodeURIComponent(deny.policyId)}`;
  return `/v2/policy?policy=${encodeURIComponent(deny.policyId)}`;
}

/** The 决策 tab of the engine that decided — where the card's `decisionId` is journaled;
 *  direct when the engine is known, else resolved from the policy id, else the Policy module. */
export function policyDecisionsHref(deny?: Pick<PolicyDeny, "policyId" | "engineId"> | null): string {
  if (deny?.engineId) return `/v2/policy?view=engine&engine=${encodeURIComponent(deny.engineId)}&tab=decisions`;
  if (!deny?.policyId) return "/v2/policy";
  return `/v2/policy?policy=${encodeURIComponent(deny.policyId)}&tab=decisions`;
}

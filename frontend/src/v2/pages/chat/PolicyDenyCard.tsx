import { ExternalLink, ShieldX } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { type PolicyDeny, policyDecisionsHref, policyDenyHref, policyLabel } from "../../../lib/policy-deny";
import { Tag } from "../../ui";
import { PolicyModeTag } from "../policy/widgets";

/**
 * A Gateway tool call Launchpad's policy refused, shown in the Chat thread next to the as_user
 * consent card and in the Decisions detail: the agent reached a Gateway tool and the Gateway
 * interceptor denied it for this identity (SSE `policy_denied`). Policies decide tool calls
 * only (Global parity, amendment 9) — the invoke denial card is gone with the hub gate. The
 * headline names the denying POLICY (name, else id) and the engine STAGE (阶段 A / 阶段 BT) in
 * the console's language; the raw English engine text is collapsed under a details toggle. No
 * automatic retry. N17-obs510: the 策略 row lists every determining
 * policy as its own chip (name + id), never run together.
 */
export function PolicyDenyCard({ deny }: { deny: PolicyDeny }) {
  const { t } = useTranslation();
  const { authRequired, username, role } = useAuth();
  const stage = deny.stage ? t(`v2.governance.policy.dec.stage.${deny.stage}`) : null;
  const policy = policyLabel(deny);  // N17-obs507: every policy the Gateway named
  const headline = deny.reason === "temporal_set_mismatch"
    ? deny.observed ? "observedTemporalMismatch" : "temporalMismatch"
    : deny.observed
    ? policy ? "observedByPolicy" : "observedByDefault"
    : policy
      ? stage ? "byPolicyStage" : "byPolicy"
      : stage ? "byDefaultStage" : "byDefault";
  return (
    <div className="v2-chat-auth v2-chat-policy" data-testid="policy-deny-card">
      <div className="v2-chat-auth-head">
        <ShieldX size={15} aria-hidden="true" />
        <strong>{t("v2.chat.policyDeny.title")}</strong>
        <span className="mono">{deny.tool || "—"}</span>
        <Tag tone={deny.observed ? "orange" : "red"}>{t(deny.observed ? "v2.chat.policyDeny.observed" : "v2.chat.policyDeny.denied")}</Tag>
        {stage && (
          <Tag tone="outline" testId="policy-deny-stage">
            {stage}
          </Tag>
        )}
        {deny.mode && <PolicyModeTag mode={deny.mode} testId="policy-deny-mode" />}
      </div>
      <div className="v2-chat-auth-body">
        <div data-testid="policy-deny-summary">
          <b>{t(`v2.chat.policyDeny.${headline}`, { policy: policy ?? "", stage: stage ?? "" })}</b>
        </div>
        <div>{t("v2.chat.policyDeny.body", { tool: deny.tool || "—" })}</div>
      </div>
      <dl className="v2-chat-policy-facts">
        {policy && (
          <>
            <dt>{t("v2.chat.policyDeny.policy")}</dt>
            <dd data-testid="policy-deny-policy">
              <span className="v2-tags v2-chat-policy-list">
                {(deny.policyIds && deny.policyIds.length > 0 ? deny.policyIds : deny.policyId ? [deny.policyId] : []).map((id, i) => {
                  const name = deny.policyIds && deny.policyIds.length > 0 ? deny.policyNames?.[i] : deny.policyName;
                  return (
                    <Tag key={id} tone="outline" testId={`policy-deny-policy-${id}`}>
                      {name && name !== id ? <span>{name} </span> : null}
                      <span className="mono">{id}</span>
                    </Tag>
                  );
                })}
                {!deny.policyId && (!deny.policyIds || deny.policyIds.length === 0) && deny.policyName ? <span>{deny.policyName}</span> : null}
              </span>
            </dd>
          </>
        )}
        {deny.source && (
          <>
            <dt>{t("v2.chat.policyDeny.decidedBy")}</dt>
            <dd data-testid="policy-deny-source">{t(`v2.chat.policyDeny.source.${deny.source}`)}</dd>
          </>
        )}
        {deny.decisionId && (
          <>
            <dt>{t("v2.chat.policyDeny.decision")}</dt>
            <dd className="mono">{deny.decisionId}</dd>
          </>
        )}
        {deny.agentId && (
          <>
            <dt>{t("v2.chat.policyDeny.agent")}</dt>
            <dd className="mono" data-testid="policy-deny-agent">
              {deny.agentId}
            </dd>
          </>
        )}
        {deny.principal ? (
          // N17-obs509: a Decisions row names whose call was decided — never the viewer
          <>
            <dt>{t("v2.chat.policyDeny.identity")}</dt>
            <dd data-testid="policy-deny-identity">
              <span className="mono">{deny.principal.name}</span>
              {deny.principal.role ? ` · ${deny.principal.role}` : ""}
            </dd>
          </>
        ) : authRequired && username ? (
          // a live / restored chat frame: the turn's user is the viewer
          <>
            <dt>{t("v2.chat.policyDeny.identity")}</dt>
            <dd data-testid="policy-deny-identity">
              <span className="mono">{username}</span>
              {role ? ` · ${role}` : ""}
            </dd>
          </>
        ) : null}
      </dl>
      {deny.reason && (
        <details className="v2-chat-policy-raw" data-testid="policy-deny-raw">
          <summary data-testid="policy-deny-raw-toggle">{t("v2.chat.policyDeny.rawToggle")}</summary>
          <code data-testid="policy-deny-reason">{deny.reason}</code>
        </details>
      )}
      <div className="v2-chat-auth-actions">
        <Link className="v2-btn sm" to={policyDenyHref(deny)} data-testid="policy-deny-link">
          <ExternalLink size={13} aria-hidden="true" />
          {t(deny.policyId ? "v2.chat.policyDeny.openPolicy" : "v2.chat.policyDeny.openPolicies")}
        </Link>
        {deny.decisionId && (
          <Link className="v2-btn sm" to={policyDecisionsHref(deny)} data-testid="policy-deny-decisions-link">
            {t("v2.chat.policyDeny.openDecisions")}
          </Link>
        )}
      </div>
    </div>
  );
}

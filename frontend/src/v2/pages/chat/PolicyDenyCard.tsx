import { ExternalLink, ShieldX } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { type PolicyDeny, policyDenyHref } from "../../../lib/policy-deny";
import { Tag } from "../../ui";

/**
 * A Gateway tool call a Cedar policy denied, shown in the Chat thread next to
 * the as_user consent card: the agent reached the tool, the Gateway refused it
 * for the signed-in identity. No retry — a deny holds until the rule changes.
 */
export function PolicyDenyCard({ deny }: { deny: PolicyDeny }) {
  const { t } = useTranslation();
  const { authRequired, username, role } = useAuth();
  return (
    <div className="v2-chat-auth v2-chat-policy" data-testid="policy-deny-card">
      <div className="v2-chat-auth-head">
        <ShieldX size={15} aria-hidden="true" />
        <strong>{t("v2.chat.policy.title")}</strong>
        <span className="mono">{deny.tool || "—"}</span>
        <Tag tone="red">{t("v2.chat.policy.denied")}</Tag>
      </div>
      <div className="v2-chat-auth-body">{t("v2.chat.policy.body", { tool: deny.tool || "—" })}</div>
      <dl className="v2-chat-policy-facts">
        <dt>{t("v2.chat.policy.reason")}</dt>
        <dd data-testid="policy-deny-reason">{deny.reason || "—"}</dd>
        {deny.policyId && (
          <>
            <dt>{t("v2.chat.policy.policy")}</dt>
            <dd className="mono">{deny.policyId}</dd>
          </>
        )}
        {authRequired && username && (
          <>
            <dt>{t("v2.chat.policy.identity")}</dt>
            <dd>
              <span className="mono">{username}</span>
              {role ? ` · ${role}` : ""}
            </dd>
          </>
        )}
      </dl>
      <div className="v2-chat-auth-actions">
        <Link className="v2-btn sm" to={policyDenyHref(deny)} data-testid="policy-deny-link">
          <ExternalLink size={13} aria-hidden="true" />
          {t(deny.gatewayId ? "v2.chat.policy.openPolicies" : "v2.chat.policy.openGovernance")}
        </Link>
      </div>
    </div>
  );
}

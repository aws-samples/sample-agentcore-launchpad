import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { api, errorMessage, type UserGrantInfo } from "../../lib/api";
import { revokeDisabled, runRevoke } from "../../lib/user-grants";
import { fmtTime } from "../format";
import { useLoad, useV2Toast } from "../hooks";
import { Alert, Card, Confirm, LinkButton, PageHeader, Table, Tag, type TagTone } from "../ui";
import "./connections/connections.css";

const STATUS_TONE: Record<UserGrantInfo["status"], TagTone> = {
  authorized: "green",
  pending: "orange",
  revoked: "gray",
};

/**
 * 我的授权 (My authorizations) — the signed-in user's own as_user (3LO) grants: which agent may call
 * which Connection as them. Revoking forces a fresh consent on that
 * Connection's next call for every agent (AgentCore Identity has no revoke
 * API; the platform sends forceAuthentication until the user re-authorizes).
 */
export function V2MyConnections() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { can } = useAuth();
  const mayRevoke = can("identity.grant");
  const grants = useLoad(() => api.listMyGrants(), "my-grants");
  const [confirm, setConfirm] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);

  const revoke = (connection: string) =>
    runRevoke(connection, {
      revoke: api.revokeUserToken,
      setBusy: setRevoking,
      onDone: (provider) => {
        toast("success", t("v2.myConnections.revoked", { connection: provider }));
        setConfirm(null);
        grants.reload();
      },
      onError: (err) => toast("error", errorMessage(err)),
    });

  const rows = grants.data?.grants ?? [];
  const affected = confirm ? rows.filter((g) => g.connection === confirm).length : 0;

  return (
    <>
      <PageHeader title={t("v2.myConnections.title")} desc={t("v2.myConnections.desc")} />
      {!mayRevoke && <Alert tone="info">{t("v2.myConnections.noPermission")}</Alert>}
      <Card title={t("v2.myConnections.listTitle")} sub={rows.length ? String(rows.length) : undefined} flush>
        <Table<UserGrantInfo>
          testId="my-grants"
          rows={rows}
          rowKey={(g) => `${g.connection}/${g.agent_id}`}
          loading={grants.loading}
          error={grants.error}
          onRetry={grants.reload}
          empty={t("v2.myConnections.empty")}
          columns={[
            { key: "connection", title: t("v2.myConnections.colConnection"), render: (g) => <span className="mono">{g.connection}</span> },
            {
              key: "agent",
              title: t("v2.myConnections.colAgent"),
              render: (g) =>
                g.agent_id ? (
                  <Link to={`/v2/agents?view=detail&id=${encodeURIComponent(g.agent_id)}`}>
                    {g.agent_name ?? g.agent_id}
                  </Link>
                ) : (
                  "—"
                ),
            },
            { key: "tool", title: t("v2.myConnections.colTool"), render: (g) => <span className="mono">{g.tool || "—"}</span> },
            {
              key: "scopes",
              title: t("v2.myConnections.colScopes"),
              render: (g) =>
                g.scopes.length ? (
                  <span className="v2-grant-scopes">
                    {g.scopes.map((s) => (
                      <code key={s}>{s}</code>
                    ))}
                  </span>
                ) : (
                  "—"
                ),
            },
            {
              key: "status",
              title: t("v2.myConnections.colStatus"),
              render: (g) => (
                <span className="v2-row" data-testid={`grant-status-${g.connection}-${g.agent_id}`}>
                  <Tag tone={STATUS_TONE[g.status]} dot>
                    {t(`v2.myConnections.status.${g.status}`)}
                  </Tag>
                  {g.force_reauth && g.status !== "revoked" && (
                    <Tag tone="outline">{t("v2.myConnections.forceReauth")}</Tag>
                  )}
                </span>
              ),
            },
            { key: "authorized", title: t("v2.myConnections.colAuthorized"), render: (g) => fmtTime(g.authorized_at) },
            {
              key: "actions",
              title: "",
              render: (g) => (
                <LinkButton
                  danger
                  disabled={revokeDisabled(g, mayRevoke, revoking)}
                  title={mayRevoke ? undefined : t("v2.myConnections.noPermission")}
                  onClick={() => setConfirm(g.connection)}
                  testId={`grant-revoke-${g.connection}-${g.agent_id}`}
                >
                  {t("v2.myConnections.revoke")}
                </LinkButton>
              ),
            },
          ]}
        />
      </Card>
      <Confirm
        open={confirm !== null}
        title={t("v2.myConnections.confirmTitle", { connection: confirm ?? "" })}
        body={t("v2.myConnections.confirmBody", { connection: confirm ?? "", count: affected })}
        confirmLabel={revoking ? t("v2.myConnections.revoking") : t("v2.myConnections.revoke")}
        danger
        busy={revoking !== null}
        onConfirm={() => {
          if (confirm) void revoke(confirm);
        }}
        onClose={() => {
          if (revoking === null) setConfirm(null);
        }}
      />
    </>
  );
}

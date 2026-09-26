import { KeyRound, ShieldCheck, Trash2, UserCheck, UserX } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { AGENT_PERMISSIONS, api } from "../../../lib/api";
import { useWorkspace } from "../../../workspace/workspace-context";
import { fmtTime } from "../../format";
import { useLoad } from "../../hooks";
import { Alert, Button, Card, Descriptions, FlowHeader, LinkButton, Spin } from "../../ui";
import { useUserActions } from "./actions";
import { GrantChip, RoleTag, StateTag, ValidityCell } from "./tags";

/**
 * One account (`?view=detail&user=<username>`): profile, validity, role, agent
 * permissions and workspace grants. There is no GET-by-id route, so the account
 * is read back through the list search and matched on the exact username
 * (usernames are unique).
 */
export function UserDetail({ username }: { username: string }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const { workspaces } = useWorkspace();
  const loaded = useLoad(async () => {
    const page = await api.listUsers({ q: username, limit: 200 });
    return page.items.find((u) => u.username === username) ?? null;
  }, `user:${username}`);
  const actions = useUserActions(loaded.reload);
  const back = () => setParams({});

  const user = loaded.data;
  if (!user) {
    return (
      <>
        <FlowHeader title={username} onBack={back} />
        {loaded.loading ? (
          <Card>
            <Spin />
          </Card>
        ) : (
          <Alert
            tone="error"
            action={<LinkButton onClick={loaded.reload}>{t("v2.common.retry")}</LinkButton>}
          >
            {loaded.error
              ? t("usersPage.loadFailed", { msg: loaded.error })
              : t("v2.users.notFound", { username })}
          </Alert>
        )}
      </>
    );
  }

  const busy = actions.busyId === user.id;
  const isAdminRole = user.role === "admin";

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {user.username}
            <RoleTag role={user.role} />
            <StateTag state={user.state} />
          </span>
        }
        onBack={back}
        end={
          <>
            {user.state === "pending" ? (
              <>
                <Button disabled={busy} onClick={() => actions.ask("reject", user)} testId="v2-users-detail-reject">
                  <UserX size={14} aria-hidden="true" />
                  {t("v2.users.action.reject")}
                </Button>
                <Button kind="primary" disabled={busy} onClick={() => actions.openApprove(user)} testId="v2-users-detail-approve">
                  <UserCheck size={14} aria-hidden="true" />
                  {t("v2.users.action.approve")}
                </Button>
              </>
            ) : (
              <Button disabled={busy} onClick={() => actions.toggleStatus(user)} testId="v2-users-detail-toggle">
                {user.status === "active" ? <UserX size={14} aria-hidden="true" /> : <UserCheck size={14} aria-hidden="true" />}
                {t(user.status === "active" ? "v2.users.action.disable" : "v2.users.action.enable")}
              </Button>
            )}
            <Button disabled={busy} onClick={() => actions.ask("reset", user)} testId="v2-users-detail-reset">
              <KeyRound size={14} aria-hidden="true" />
              {t("v2.users.action.resetPassword")}
            </Button>
            <Button kind="danger" disabled={busy} onClick={() => actions.ask("delete", user)} testId="v2-users-detail-delete">
              <Trash2 size={14} aria-hidden="true" />
              {t("v2.users.action.delete")}
            </Button>
          </>
        }
      />

      {user.state === "pending" && <Alert tone="info">{t("v2.users.pendingAlert")}</Alert>}
      {user.state === "expired" && <Alert tone="warn">{t("v2.users.expiredAlert")}</Alert>}
      {user.state === "disabled" && <Alert tone="warn">{t("v2.users.disabledAlert")}</Alert>}

      <Card title={t("v2.users.overview")} testId="v2-users-overview">
        <Descriptions
          items={[
            { label: t("v2.users.field.username"), value: user.username },
            { label: t("v2.users.field.email"), value: user.email },
            { label: t("v2.users.col.role"), value: <RoleTag role={user.role} /> },
            { label: t("v2.users.col.state"), value: <StateTag state={user.state} /> },
            { label: t("v2.users.col.validity"), value: <ValidityCell user={user} /> },
            { label: t("v2.users.col.created"), value: fmtTime(user.created_at) },
            { label: t("v2.users.col.lastLogin"), value: fmtTime(user.last_login_at) },
            { label: t("v2.users.field.logins"), value: user.login_count },
            { label: t("v2.users.field.createdBy"), value: user.created_by || "—" },
            { label: "ID", value: <span className="mono">{user.id}</span> },
          ]}
        />
      </Card>

      <div className="v2-users-grid">
        <Card title={t("v2.users.validityTitle")} sub={t("v2.users.validitySub")} testId="v2-users-validity">
          <div className="v2-row">
            <Button disabled={busy} onClick={() => void actions.patch(user, { extend_days: 7 }, "usersPage.extended")} testId="v2-users-extend7">
              {t("v2.users.extendBy", { count: 7 })}
            </Button>
            <Button disabled={busy} onClick={() => void actions.patch(user, { extend_days: 30 }, "usersPage.extended")} testId="v2-users-extend30">
              {t("v2.users.extendBy", { count: 30 })}
            </Button>
            <Button disabled={busy} onClick={() => actions.openExtend(user)} testId="v2-users-extend-custom">
              {t("v2.users.extendCustom")}
            </Button>
          </div>
        </Card>

        <Card title={t("v2.users.roleTitle")} sub={t("v2.users.roleSub")} testId="v2-users-role">
          <div className="v2-row">
            <RoleTag role={user.role} />
            <Button disabled={busy} onClick={() => actions.ask("role", user)} testId="v2-users-role-toggle">
              <ShieldCheck size={14} aria-hidden="true" />
              {t(isAdminRole ? "v2.users.action.makeMember" : "v2.users.action.makeAdmin")}
            </Button>
          </div>
        </Card>
      </div>

      <Card title={t("v2.users.permTitle")} sub={t("v2.users.permSub")} testId="v2-users-perms">
        {isAdminRole ? (
          <span className="v2-muted">{t("v2.users.allByRole")}</span>
        ) : (
          <div className="v2-users-chips">
            {AGENT_PERMISSIONS.map((key) => {
              const granted = user.permissions?.[key] !== false;
              return (
                <GrantChip
                  key={key}
                  on={granted}
                  disabled={busy}
                  title={key}
                  testId={`v2-users-perm-${key}`}
                  onClick={() =>
                    void actions.patch(user, { permissions: { [key]: !granted } }, "usersPage.permissionsUpdated")
                  }
                >
                  {t(`v2.users.perm.${key.split(".")[1]}`)}
                  <small className="mono">{key}</small>
                </GrantChip>
              );
            })}
          </div>
        )}
      </Card>

      <Card title={t("v2.users.wsTitle")} sub={t("v2.users.wsSub")} testId="v2-users-workspaces">
        {isAdminRole ? (
          // Admins reach every workspace by role; a grant row for them would
          // suggest access that could be revoked.
          <span className="v2-muted">{t("v2.users.allByRole")}</span>
        ) : workspaces.length === 0 ? (
          <span className="v2-muted">—</span>
        ) : (
          <div className="v2-users-chips">
            {workspaces.map((ws) => {
              const granted = user.workspaces.includes(ws.id);
              return (
                <GrantChip
                  key={ws.id}
                  on={granted}
                  disabled={busy}
                  title={`${ws.account_id} · ${ws.region}`}
                  testId={`v2-users-ws-${ws.id}`}
                  onClick={() =>
                    void actions.patch(
                      user,
                      {
                        workspaces: granted
                          ? user.workspaces.filter((id) => id !== ws.id)
                          : [...user.workspaces, ws.id],
                      },
                      "usersPage.workspacesUpdated",
                    )
                  }
                >
                  {ws.name || ws.id}
                  <small className="mono">{ws.id}</small>
                </GrantChip>
              );
            })}
          </div>
        )}
      </Card>

      {actions.renderDialogs({ onDeleted: back })}
    </>
  );
}

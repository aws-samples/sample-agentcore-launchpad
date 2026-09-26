import { Copy } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type ConsoleUser, errorMessage, type UserPatchBody } from "../../../lib/api";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Confirm, Field, Modal } from "../../ui";
import { clampDays, DEFAULT_GRANT, MAX_EXTEND_DAYS } from "./common";
import { GrantChip } from "./tags";

/** Account actions that change access, so each one asks first. */
export type ConfirmKind = "delete" | "disable" | "reject" | "reset" | "role";

/**
 * Every account write of the page (list and detail share it): the PATCH/DELETE
 * calls with busy + toast handling, and the dialogs they need — approve with
 * workspace grants, custom extension, confirmations, and the one-time
 * generated password.
 */
export function useUserActions(onChanged: () => void) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { workspaces } = useWorkspace();
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ kind: ConfirmKind; user: ConsoleUser } | null>(null);
  const [extendTarget, setExtendTarget] = useState<ConsoleUser | null>(null);
  const [extendDays, setExtendDays] = useState("14");
  const [approveTarget, setApproveTarget] = useState<ConsoleUser | null>(null);
  const [approveGrants, setApproveGrants] = useState<string[]>([]);
  const [reset, setReset] = useState<{ username: string; password: string } | null>(null);

  const patch = async (user: ConsoleUser, body: UserPatchBody, successKey: string) => {
    setBusyId(user.id);
    try {
      const updated = await api.updateUser(user.id, body);
      if (updated.generated_password) {
        setReset({ username: updated.username, password: updated.generated_password });
      }
      toast("success", t(successKey, { username: user.username }));
      onChanged();
      return true;
    } catch (err) {
      toast("error", t("usersPage.actionFailed", { msg: errorMessage(err) }));
      return false;
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (user: ConsoleUser) => {
    setBusyId(user.id);
    try {
      await api.deleteUser(user.id);
      toast("success", t("usersPage.deleted", { username: user.username }));
      onChanged();
      return true;
    } catch (err) {
      toast("error", t("usersPage.actionFailed", { msg: errorMessage(err) }));
      return false;
    } finally {
      setBusyId(null);
    }
  };

  const toggleStatus = (user: ConsoleUser) =>
    user.status === "active"
      ? setConfirm({ kind: "disable", user })
      : void patch(user, { status: "active" }, "usersPage.enabled");

  const openExtend = (user: ConsoleUser) => {
    setExtendDays("14");
    setExtendTarget(user);
  };

  // Approval decides the account's environments too, so it asks instead of
  // granting the hub silently.
  const openApprove = (user: ConsoleUser) => {
    setApproveGrants(user.workspaces.length > 0 ? user.workspaces : [DEFAULT_GRANT]);
    setApproveTarget(user);
  };

  const ask = (kind: ConfirmKind, user: ConsoleUser) => setConfirm({ kind, user });

  const runConfirm = async (onDeleted?: () => void) => {
    if (!confirm) return;
    const { kind, user } = confirm;
    let ok = false;
    if (kind === "delete") {
      ok = await remove(user);
      if (ok) onDeleted?.();
    } else if (kind === "disable") {
      ok = await patch(user, { status: "disabled" }, "usersPage.disabled");
    } else if (kind === "reject") {
      ok = await patch(user, { status: "disabled" }, "usersPage.rejected");
    } else if (kind === "reset") {
      ok = await patch(user, { password: null }, "usersPage.passwordReset");
    } else {
      ok = await patch(
        user,
        { role: user.role === "admin" ? "member" : "admin" },
        "v2.users.roleUpdated",
      );
    }
    if (ok) setConfirm(null);
  };

  const confirmCopy = (kind: ConfirmKind, user: ConsoleUser) => {
    const username = user.username;
    switch (kind) {
      case "delete":
        return {
          title: t("usersPage.deleteTitle"),
          body: t("usersPage.deleteBody", { username }),
          label: t("v2.users.action.delete"),
          danger: true,
        };
      case "disable":
        return {
          title: t("v2.users.confirm.disableTitle"),
          body: t("v2.users.confirm.disableBody", { username }),
          label: t("v2.users.action.disable"),
          danger: true,
        };
      case "reject":
        return {
          title: t("v2.users.confirm.rejectTitle"),
          body: t("v2.users.confirm.rejectBody", { username }),
          label: t("v2.users.action.reject"),
          danger: true,
        };
      case "reset":
        return {
          title: t("v2.users.confirm.resetTitle"),
          body: t("v2.users.confirm.resetBody", { username }),
          label: t("v2.users.action.resetPassword"),
          danger: false,
        };
      default:
        return user.role === "admin"
          ? {
              title: t("v2.users.confirm.demoteTitle"),
              body: t("v2.users.confirm.demoteBody", { username }),
              label: t("v2.users.action.makeMember"),
              danger: true,
            }
          : {
              title: t("v2.users.confirm.promoteTitle"),
              body: t("v2.users.confirm.promoteBody", { username }),
              label: t("v2.users.action.makeAdmin"),
              danger: false,
            };
    }
  };

  const renderDialogs = (opts: { onDeleted?: () => void } = {}) => {
    const copy = confirm ? confirmCopy(confirm.kind, confirm.user) : null;
    return (
      <>
        <Confirm
          open={copy !== null}
          title={copy?.title ?? ""}
          body={copy?.body ?? ""}
          confirmLabel={copy?.label ?? ""}
          danger={copy?.danger}
          busy={busyId !== null}
          onConfirm={() => void runConfirm(opts.onDeleted)}
          onClose={() => setConfirm(null)}
        />

        <Modal
          open={extendTarget !== null}
          title={t("usersPage.extendTitle")}
          onClose={() => setExtendTarget(null)}
          testId="v2-users-extend"
          footer={
            <>
              <Button onClick={() => setExtendTarget(null)}>{t("v2.common.cancel")}</Button>
              <Button
                kind="primary"
                disabled={busyId !== null}
                testId="v2-users-extend-ok"
                onClick={() => {
                  const target = extendTarget;
                  if (!target) return;
                  setExtendTarget(null);
                  void patch(target, { extend_days: clampDays(extendDays) }, "usersPage.extended");
                }}
              >
                {t("v2.users.action.extend")}
              </Button>
            </>
          }
        >
          <p className="v2-users-dialog-body">
            {t("usersPage.extendBody", { username: extendTarget?.username ?? "" })}
          </p>
          <Field label={t("v2.users.extendDays")} hint={t("v2.users.extendHint", { max: MAX_EXTEND_DAYS })}>
            <input
              className="v2-input"
              type="number"
              min={1}
              max={MAX_EXTEND_DAYS}
              value={extendDays}
              onChange={(e) => setExtendDays(e.target.value)}
              data-testid="v2-users-extend-days"
            />
          </Field>
        </Modal>

        <Modal
          open={approveTarget !== null}
          title={t("usersPage.approveTitle")}
          onClose={() => setApproveTarget(null)}
          testId="v2-users-approve"
          footer={
            <>
              <Button onClick={() => setApproveTarget(null)}>{t("v2.common.cancel")}</Button>
              <Button
                kind="primary"
                disabled={busyId !== null}
                testId="v2-users-approve-ok"
                onClick={() => {
                  const target = approveTarget;
                  if (!target) return;
                  setApproveTarget(null);
                  void patch(
                    target,
                    { status: "active", workspaces: approveGrants },
                    "usersPage.approved",
                  );
                }}
              >
                {t("v2.users.action.approve")}
              </Button>
            </>
          }
        >
          <p className="v2-users-dialog-body">
            {t("usersPage.approveBody", { username: approveTarget?.username ?? "" })}
          </p>
          <Field label={t("v2.users.col.workspaces")} hint={t("v2.users.approveHint")}>
            <div className="v2-users-chips" data-testid="v2-users-approve-workspaces">
              {workspaces.map((ws) => {
                const on = approveGrants.includes(ws.id);
                return (
                  <GrantChip
                    key={ws.id}
                    on={on}
                    title={ws.name}
                    testId={`v2-users-approve-ws-${ws.id}`}
                    onClick={() =>
                      setApproveGrants((prev) =>
                        on ? prev.filter((id) => id !== ws.id) : [...prev, ws.id],
                      )
                    }
                  >
                    {ws.id}
                  </GrantChip>
                );
              })}
              {workspaces.length === 0 && <span className="v2-muted">—</span>}
            </div>
          </Field>
          {approveGrants.length === 0 && <Alert tone="warn">{t("v2.users.approveNoGrant")}</Alert>}
        </Modal>

        <Modal
          open={reset !== null}
          title={t("usersPage.resetTitle")}
          onClose={() => setReset(null)}
          testId="v2-users-password"
          footer={
            <>
              <Button
                onClick={() => {
                  if (!reset) return;
                  void navigator.clipboard?.writeText(reset.password);
                  toast("success", t("usersPage.copied"));
                }}
              >
                <Copy size={14} aria-hidden="true" />
                {t("v2.users.copy")}
              </Button>
              <Button kind="primary" onClick={() => setReset(null)}>
                {t("v2.common.close")}
              </Button>
            </>
          }
        >
          <Alert tone="warn">{t("usersPage.resetBody", { username: reset?.username ?? "" })}</Alert>
          <code className="v2-users-password" data-testid="v2-users-generated-password">
            {reset?.password}
          </code>
        </Modal>
      </>
    );
  };

  return { busyId, patch, ask, toggleStatus, openExtend, openApprove, renderDialogs };
}

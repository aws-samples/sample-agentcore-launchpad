import { LoaderCircle, RefreshCw } from "lucide-react";
import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import {
  AdminRequired,
  Btn,
  Chip,
  type ChipTone,
  type Column,
  ConfirmDialog,
  DataTable,
  Panel,
  StatTile,
  useToast,
  ViewHead,
} from "../components";
import type { ConsoleUser, UserStats, UserStatusFilter } from "../lib/api";
import { AGENT_PERMISSIONS, api } from "../lib/api";

const STATUS_FILTERS: UserStatusFilter[] = [
  "all",
  "pending",
  "active",
  "expired",
  "disabled",
];
const PAGE_SIZE = 25;

const STATE_TONE: Record<ConsoleUser["state"], ChipTone> = {
  pending: "blue",
  active: "good",
  expired: "amber",
  disabled: "muted",
};

function fmt(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

/**
 * Admin-only account management: registration statistics plus the account table
 * (extend validity, disable/enable, reset password, delete).
 *
 * Filter + paging state lives in `?status=`/`?q=`/`?page=` so a filtered view is
 * reload- and link-safe, matching the other console list surfaces.
 */
export function Users() {
  const { t } = useTranslation();
  const toast = useToast();
  const { isAdmin } = useAuth();
  const [params, setParams] = useSearchParams();

  const statusParam = params.get("status") as UserStatusFilter | null;
  const status: UserStatusFilter =
    statusParam && STATUS_FILTERS.includes(statusParam) ? statusParam : "all";
  const query = params.get("q") ?? "";
  const page = Math.max(1, Number(params.get("page") ?? "1") || 1);

  const [rows, setRows] = useState<ConsoleUser[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<UserStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<ConsoleUser | null>(null);
  const [extendTarget, setExtendTarget] = useState<ConsoleUser | null>(null);
  const [extendDays, setExtendDays] = useState("14");
  const [reset, setReset] = useState<{ username: string; password: string } | null>(null);
  const seq = useRef(0);

  const load = useCallback(() => {
    if (!isAdmin) return;
    const id = ++seq.current;
    setLoading(true);
    setError(null);
    Promise.all([
      api.listUsers({
        q: query || undefined,
        status,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      }),
      api.userStats(),
    ])
      .then(([list, summary]) => {
        if (id !== seq.current) return; // ignore out-of-order responses
        setRows(list.items);
        setTotal(list.total);
        setStats(summary);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (id !== seq.current) return;
        const msg = err instanceof Error ? err.message : String(err);
        setError(msg);
        setLoading(false);
        toast(t("usersPage.loadFailed", { msg }), "crit");
      });
  }, [isAdmin, page, query, status, t, toast]);

  useEffect(load, [load]);

  const setParam = (key: string, value: string | null) => {
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      if (value) next.set(key, value);
      else next.delete(key);
      if (key !== "page") next.delete("page"); // any filter change restarts paging
      return next;
    });
  };

  const patch = async (
    user: ConsoleUser,
    body: Parameters<typeof api.updateUser>[1],
    successKey: string,
  ) => {
    setBusyId(user.id);
    try {
      const updated = await api.updateUser(user.id, body);
      if (updated.generated_password) {
        setReset({ username: updated.username, password: updated.generated_password });
      }
      toast(t(successKey, { username: user.username }), "good");
      load();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast(t("usersPage.actionFailed", { msg }), "crit");
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (user: ConsoleUser) => {
    setBusyId(user.id);
    try {
      await api.deleteUser(user.id);
      toast(t("usersPage.deleted", { username: user.username }), "good");
      load();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast(t("usersPage.actionFailed", { msg }), "crit");
    } finally {
      setBusyId(null);
      setPendingDelete(null);
    }
  };

  const maxRegistrations = useMemo(
    () => Math.max(1, ...(stats?.registrations ?? []).map((point) => point.count)),
    [stats],
  );
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  if (!isAdmin) {
    return (
      <AdminRequired
        kicker={t("usersPage.kicker")}
        title={t("usersPage.title")}
        body={t("usersPage.forbiddenBody")}
        testId="users-forbidden-body"
      />
    );
  }

  const columns: Column[] = [
    { key: "user", label: t("usersPage.cols.user") },
    { key: "role", label: t("usersPage.cols.role") },
    { key: "permissions", label: t("usersPage.cols.permissions") },
    { key: "state", label: t("usersPage.cols.state") },
    { key: "validity", label: t("usersPage.cols.validity") },
    { key: "created", label: t("usersPage.cols.created") },
    { key: "lastLogin", label: t("usersPage.cols.lastLogin") },
    { key: "logins", label: t("usersPage.cols.logins") },
    { key: "actions", label: t("usersPage.cols.actions") },
  ];

  return (
    <>
      <ViewHead
        kicker={t("usersPage.kicker")}
        title={t("usersPage.title")}
        meta={t("usersPage.meta", { days: stats?.valid_days ?? 7 })}
      />

      <div className="tiles five">
        <StatTile
          label={t("usersPage.stats.pending")}
          value={stats?.pending ?? "—"}
          foot={t("usersPage.stats.pendingFoot")}
        />
        <StatTile
          label={t("usersPage.stats.total")}
          value={stats?.total ?? "—"}
          foot={t("usersPage.stats.registeredLast7d", {
            count: stats?.registered_last_7d ?? 0,
          })}
        />
        <StatTile
          label={t("usersPage.stats.active")}
          value={stats?.active ?? "—"}
          foot={t("usersPage.stats.activeLast7d", { count: stats?.active_last_7d ?? 0 })}
        />
        <StatTile
          label={t("usersPage.stats.expiringSoon")}
          value={stats?.expiring_soon ?? "—"}
          foot={t("usersPage.stats.expiringSoonFoot")}
        />
        <StatTile
          label={t("usersPage.stats.expiredDisabled")}
          value={
            stats ? stats.expired + stats.disabled : "—"
          }
          foot={t("usersPage.stats.expiredDisabledFoot", {
            expired: stats?.expired ?? 0,
            disabled: stats?.disabled ?? 0,
          })}
        />
      </div>

      <Panel
        brk
        title={t("usersPage.trendTitle")}
        sub={t("usersPage.trendSub")}
        style={{ "--i": 0 } as CSSProperties}
      >
        <div className="reg-trend" data-testid="registration-trend">
          {(stats?.registrations ?? []).map((point) => (
            <div className="reg-col" key={point.date} title={`${point.date} · ${point.count}`}>
              <div
                className="reg-bar"
                style={{ height: `${Math.round((point.count / maxRegistrations) * 100)}%` }}
              />
              <span>{point.date.slice(5)}</span>
            </div>
          ))}
        </div>
        {stats && stats.top_domains.length > 0 ? (
          <div className="domain-list">
            <span className="dl-label">{t("usersPage.topDomains")}</span>
            {stats.top_domains.map((entry) => (
              <Chip key={entry.domain} tone="blue">
                {entry.domain} · {entry.count}
              </Chip>
            ))}
          </div>
        ) : null}
      </Panel>

      <Panel brk pad={false} style={{ "--i": 1 } as CSSProperties}>
        <div className="filters">
          {STATUS_FILTERS.map((option) => (
            <button
              key={option}
              className={`fsel${status === option ? " on-ok" : ""}`}
              onClick={() => setParam("status", option === "all" ? null : option)}
              data-testid={`users-filter-${option}`}
            >
              {t(`usersPage.filters.${option}`)}
            </button>
          ))}
          <input
            className="fsearch"
            value={query}
            onChange={(event) => setParam("q", event.target.value || null)}
            placeholder={t("usersPage.searchPlaceholder")}
            data-testid="users-search"
          />
          <Btn onClick={load} disabled={loading} data-testid="users-refresh">
            {loading ? (
              <LoaderCircle className="spin" size={14} aria-hidden="true" />
            ) : (
              <RefreshCw size={14} aria-hidden="true" />
            )}
            {t("usersPage.refresh")}
          </Btn>
          <Chip tone="muted">{t("usersPage.count", { count: total })}</Chip>
        </div>

        <DataTable
          columns={columns}
          isEmpty={!loading && rows.length === 0}
          empty={error ?? t("usersPage.empty")}
        >
          {rows.map((user) => (
            <tr key={user.id} data-testid={`user-row-${user.username}`}>
              <td>
                <b>{user.username}</b>
                <br />
                <small className="dim">{user.email}</small>
              </td>
              <td>
                <Chip tone={user.role === "admin" ? "aqua" : "muted"}>
                  {t(user.role === "admin" ? "auth.roleAdmin" : "auth.roleMember")}
                </Chip>
              </td>
              <td>
                {user.role === "admin" ? (
                  <span className="dim mono">{t("usersPage.permissionsAll")}</span>
                ) : (
                  <div className="selchips" data-testid={`user-perms-${user.username}`}>
                    {AGENT_PERMISSIONS.map((key) => {
                      const granted = user.permissions?.[key] !== false;
                      return (
                        <button
                          key={key}
                          type="button"
                          className={`selchip${granted ? " on" : ""}`}
                          style={{ cursor: "pointer" }}
                          disabled={busyId === user.id}
                          title={t("usersPage.permissionsHint")}
                          data-testid={`user-perm-${user.username}-${key}`}
                          onClick={() =>
                            patch(
                              user,
                              { permissions: { [key]: !granted } },
                              "usersPage.permissionsUpdated",
                            )
                          }
                        >
                          {t(`usersPage.perm.${key.split(".")[1]}`)}
                        </button>
                      );
                    })}
                  </div>
                )}
              </td>
              <td>
                <Chip tone={STATE_TONE[user.state]}>
                  {t(`usersPage.filters.${user.state}`)}
                </Chip>
              </td>
              <td>
                {user.expires_at ? (
                  <>
                    {fmt(user.expires_at)}
                    <br />
                    <small className="dim">
                      {t("usersPage.daysRemaining", { count: user.days_remaining ?? 0 })}
                    </small>
                  </>
                ) : (
                  t(
                    user.state === "pending"
                      ? "usersPage.startsOnApproval"
                      : "usersPage.neverExpires",
                  )
                )}
              </td>
              <td>{fmt(user.created_at)}</td>
              <td>{fmt(user.last_login_at)}</td>
              <td>{user.login_count}</td>
              <td className="user-actions">
                {user.state === "pending" ? (
                  <>
                    <Btn
                      primary
                      disabled={busyId === user.id}
                      onClick={() =>
                        patch(user, { status: "active" }, "usersPage.approved")
                      }
                      data-testid={`user-approve-${user.username}`}
                    >
                      {t("usersPage.actions.approve")}
                    </Btn>
                    <Btn
                      disabled={busyId === user.id}
                      onClick={() =>
                        patch(user, { status: "disabled" }, "usersPage.rejected")
                      }
                      data-testid={`user-reject-${user.username}`}
                    >
                      {t("usersPage.actions.reject")}
                    </Btn>
                  </>
                ) : null}
                <Btn
                  disabled={busyId === user.id}
                  onClick={() => patch(user, { extend_days: 7 }, "usersPage.extended")}
                  data-testid={`user-extend7-${user.username}`}
                >
                  +7d
                </Btn>
                <Btn
                  disabled={busyId === user.id}
                  onClick={() => patch(user, { extend_days: 30 }, "usersPage.extended")}
                >
                  +30d
                </Btn>
                <Btn
                  disabled={busyId === user.id}
                  onClick={() => {
                    setExtendDays("14");
                    setExtendTarget(user);
                  }}
                >
                  {t("usersPage.actions.custom")}
                </Btn>
                {user.state === "pending" ? null : (
                  <Btn
                    disabled={busyId === user.id}
                    onClick={() =>
                      patch(
                        user,
                        { status: user.status === "active" ? "disabled" : "active" },
                        user.status === "active"
                          ? "usersPage.disabled"
                          : "usersPage.enabled",
                      )
                    }
                    data-testid={`user-toggle-${user.username}`}
                  >
                    {t(
                      user.status === "active"
                        ? "usersPage.actions.disable"
                        : "usersPage.actions.enable",
                    )}
                  </Btn>
                )}
                <Btn
                  disabled={busyId === user.id}
                  onClick={() => patch(user, { password: null }, "usersPage.passwordReset")}
                >
                  {t("usersPage.actions.resetPassword")}
                </Btn>
                <Btn
                  disabled={busyId === user.id}
                  onClick={() => setPendingDelete(user)}
                  data-testid={`user-delete-${user.username}`}
                >
                  {t("usersPage.actions.delete")}
                </Btn>
              </td>
            </tr>
          ))}
        </DataTable>

        {pageCount > 1 ? (
          <div className="pagerbar">
            <button
              className="fsel"
              disabled={page <= 1}
              onClick={() => setParam("page", String(page - 1))}
            >
              ← {t("usersPage.prev")}
            </button>
            <span className="dim">{t("usersPage.pageOf", { page, pages: pageCount })}</span>
            <button
              className="fsel"
              disabled={page >= pageCount}
              onClick={() => setParam("page", String(page + 1))}
            >
              {t("usersPage.next")} →
            </button>
          </div>
        ) : null}
      </Panel>

      {extendTarget ? (
        <div className="confirm-backdrop" onClick={() => setExtendTarget(null)}>
          <div
            className="confirm-box"
            role="dialog"
            aria-modal="true"
            aria-label={t("usersPage.extendTitle")}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="confirm-title">▲ {t("usersPage.extendTitle")}</div>
            <p className="confirm-body">
              {t("usersPage.extendBody", { username: extendTarget.username })}
            </p>
            <input
              className="input"
              type="number"
              min={1}
              max={3650}
              value={extendDays}
              onChange={(event) => setExtendDays(event.target.value)}
              data-testid="extend-days-input"
            />
            <div className="confirm-actions">
              <Btn onClick={() => setExtendTarget(null)}>{t("common.cancel")}</Btn>
              <Btn
                primary
                onClick={() => {
                  const days = Math.min(3650, Math.max(1, Number(extendDays) || 1));
                  const target = extendTarget;
                  setExtendTarget(null);
                  void patch(target, { extend_days: days }, "usersPage.extended");
                }}
                data-testid="extend-confirm"
              >
                {t("usersPage.actions.extend")}
              </Btn>
            </div>
          </div>
        </div>
      ) : null}

      {reset ? (
        <div className="confirm-backdrop" onClick={() => setReset(null)}>
          <div
            className="confirm-box"
            role="alertdialog"
            aria-modal="true"
            aria-label={t("usersPage.resetTitle")}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="confirm-title">▲ {t("usersPage.resetTitle")}</div>
            <p className="confirm-body">{t("usersPage.resetBody", { username: reset.username })}</p>
            <code className="reset-password" data-testid="generated-password">
              {reset.password}
            </code>
            <div className="confirm-actions">
              <Btn
                onClick={() => {
                  void navigator.clipboard?.writeText(reset.password);
                  toast(t("usersPage.copied"), "good");
                }}
              >
                {t("usersPage.copy")}
              </Btn>
              <Btn primary onClick={() => setReset(null)}>
                {t("common.close")}
              </Btn>
            </div>
          </div>
        </div>
      ) : null}

      <ConfirmDialog
        open={pendingDelete !== null}
        title={t("usersPage.deleteTitle")}
        body={t("usersPage.deleteBody", { username: pendingDelete?.username ?? "" })}
        confirmLabel={t("usersPage.actions.delete")}
        onConfirm={() => {
          if (pendingDelete) void remove(pendingDelete);
        }}
        onCancel={() => setPendingDelete(null)}
      />
    </>
  );
}

import { RefreshCw } from "lucide-react";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, type ConsoleUser, type UserStats } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad } from "../../hooks";
import {
  Button,
  Card,
  type Column,
  FilterSelect,
  Kpi,
  LinkButton,
  PageHeader,
  Pager,
  SearchInput,
  Table,
  Tag,
} from "../../ui";
import { useUserActions } from "./actions";
import { isStatusFilter, PAGE_SIZE, USER_STATES } from "./common";
import { RoleTag, StateTag, ValidityCell } from "./tags";

function RegistrationTrend({ stats }: { stats: UserStats | null }) {
  const { t } = useTranslation();
  const points = stats?.registrations ?? [];
  const max = Math.max(1, ...points.map((p) => p.count));
  return (
    <Card title={t("v2.users.trendTitle")} sub={t("usersPage.trendSub")} testId="v2-users-trend">
      <div className="v2-users-trend">
        {points.map((p) => (
          <div className="col" key={p.date} title={`${p.date} · ${p.count}`}>
            <span className="n">{p.count || ""}</span>
            <div className="bar-wrap">
              <div className="bar" style={{ height: `${Math.round((p.count / max) * 100)}%` }} />
            </div>
            <span className="d">{p.date.slice(5)}</span>
          </div>
        ))}
      </div>
      {stats && stats.top_domains.length > 0 && (
        <div className="v2-users-domains">
          <span className="v2-muted">{t("v2.users.topDomains")}</span>
          {stats.top_domains.map((entry) => (
            <Tag key={entry.domain} tone="outline">
              {entry.domain} · {entry.count}
            </Tag>
          ))}
        </div>
      )}
    </Card>
  );
}

/**
 * Account list: registration statistics, the 14-day trend and the paged account
 * table. Filter + paging live in `?status=`/`?q=`/`?page=` (the backend pages).
 */
export function UserList() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const statusParam = params.get("status");
  const status = isStatusFilter(statusParam) ? statusParam : "all";
  const query = params.get("q") ?? "";
  const page = Math.max(1, Number(params.get("page") ?? "1") || 1);

  const list = useLoad(
    () => api.listUsers({ q: query || undefined, status, limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE }),
    `users:${status}:${query}:${page}`,
  );
  const stats = useLoad(() => api.userStats(), "users-stats");
  const reload = () => {
    list.reload();
    stats.reload();
  };
  const actions = useUserActions(reload);

  const setParam = (key: string, value: string | null) =>
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(key, value);
        else next.delete(key);
        if (key !== "page") next.delete("page"); // any filter change restarts paging
        return next;
      },
      { replace: key === "q" },
    );

  const rows = useMemo(() => list.data?.items ?? [], [list.data]);
  const total = list.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const s = stats.data;
  const open = (u: ConsoleUser) => setParams({ view: "detail", user: u.username });

  const columns: Column<ConsoleUser>[] = [
    {
      key: "user",
      title: t("v2.users.col.user"),
      render: (u) => (
        <>
          <LinkButton onClick={() => open(u)} testId={`v2-users-open-${u.username}`}>
            {u.username}
          </LinkButton>
          <span className="sub">{u.email}</span>
        </>
      ),
    },
    { key: "role", title: t("v2.users.col.role"), render: (u) => <RoleTag role={u.role} /> },
    { key: "state", title: t("v2.users.col.state"), render: (u) => <StateTag state={u.state} /> },
    {
      key: "workspaces",
      title: t("v2.users.col.workspaces"),
      render: (u) =>
        u.role === "admin" ? (
          <span className="v2-muted">{t("v2.users.allByRole")}</span>
        ) : u.workspaces.length === 0 ? (
          <span className="v2-muted">—</span>
        ) : (
          <span className="v2-users-ws" title={u.workspaces.join(", ")}>
            {u.workspaces.slice(0, 2).map((id) => (
              <Tag key={id} tone="outline">
                {id}
              </Tag>
            ))}
            {u.workspaces.length > 2 && <span className="v2-muted">+{u.workspaces.length - 2}</span>}
          </span>
        ),
    },
    { key: "validity", className: "v2-users-nowrap", title: t("v2.users.col.validity"), render: (u) => <ValidityCell user={u} /> },
    {
      key: "lastLogin",
      title: t("v2.users.col.lastLogin"),
      render: (u) => (
        <>
          {fmtTime(u.last_login_at)}
          <span className="sub">{t("v2.users.logins", { count: u.login_count })}</span>
        </>
      ),
    },
    {
      key: "created",
      className: "v2-users-nowrap",
      title: t("v2.users.col.created"),
      render: (u) => <span title={fmtTime(u.created_at)}>{fmtTime(u.created_at).slice(0, 10)}</span>,
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (u) => {
        const busy = actions.busyId === u.id;
        return (
          <div className="v2-actions">
            {u.state === "pending" ? (
              <>
                <LinkButton disabled={busy} onClick={() => actions.openApprove(u)} testId={`v2-users-approve-${u.username}`}>
                  {t("v2.users.action.approve")}
                </LinkButton>
                <LinkButton danger disabled={busy} onClick={() => actions.ask("reject", u)} testId={`v2-users-reject-${u.username}`}>
                  {t("v2.users.action.reject")}
                </LinkButton>
              </>
            ) : (
              <>
                <LinkButton disabled={busy} onClick={() => actions.openExtend(u)} testId={`v2-users-extend-${u.username}`}>
                  {t("v2.users.action.extend")}
                </LinkButton>
                <LinkButton
                  danger={u.status === "active"}
                  disabled={busy}
                  onClick={() => actions.toggleStatus(u)}
                  testId={`v2-users-toggle-${u.username}`}
                >
                  {t(u.status === "active" ? "v2.users.action.disable" : "v2.users.action.enable")}
                </LinkButton>
              </>
            )}
            <LinkButton danger disabled={busy} onClick={() => actions.ask("delete", u)} testId={`v2-users-delete-${u.username}`}>
              {t("v2.users.action.delete")}
            </LinkButton>
          </div>
        );
      },
    },
  ];

  return (
    <>
      <PageHeader title={t("nav.users")} desc={t("usersPage.meta", { days: s?.valid_days ?? 7 })} />

      <div className="v2-kpis" data-testid="v2-users-kpis">
        <Kpi label={t("v2.users.stat.pending")} value={s?.pending ?? "—"} sub={t("v2.users.stat.pendingSub")} />
        <Kpi
          label={t("v2.users.stat.total")}
          value={s?.total ?? "—"}
          sub={t("usersPage.stats.registeredLast7d", { count: s?.registered_last_7d ?? 0 })}
        />
        <Kpi
          label={t("v2.users.stat.active")}
          value={s?.active ?? "—"}
          sub={t("usersPage.stats.activeLast7d", { count: s?.active_last_7d ?? 0 })}
        />
        <Kpi
          label={t("v2.users.stat.expiringSoon")}
          value={s?.expiring_soon ?? "—"}
          sub={t("usersPage.stats.expiringSoonFoot")}
        />
        <Kpi
          label={t("v2.users.stat.expiredDisabled")}
          value={s ? s.expired + s.disabled : "—"}
          sub={t("usersPage.stats.expiredDisabledFoot", { expired: s?.expired ?? 0, disabled: s?.disabled ?? 0 })}
        />
      </div>

      <RegistrationTrend stats={s} />

      <Card>
        <div className="v2-toolbar">
          <Button onClick={reload} disabled={list.loading} testId="v2-users-refresh">
            <RefreshCw size={14} aria-hidden="true" />
            {t("v2.common.refresh")}
          </Button>
          <FilterSelect
            label={t("v2.users.col.state")}
            value={status === "all" ? "" : status}
            allLabel={t("v2.common.all")}
            options={USER_STATES.map((value) => ({ value, label: t(`v2.users.state.${value}`) }))}
            onChange={(value) => setParam("status", value || null)}
            testId="v2-users-status"
          />
          <div className="end">
            <SearchInput
              value={query}
              onChange={(value) => setParam("q", value || null)}
              placeholder={t("v2.users.search")}
              testId="v2-users-search"
            />
            <span className="v2-count">{t("v2.common.total", { count: total })}</span>
          </div>
        </div>
        <Table
          columns={columns}
          rows={rows}
          rowKey={(u) => u.id}
          loading={list.loading}
          error={list.error ? t("usersPage.loadFailed", { msg: list.error }) : null}
          onRetry={list.reload}
          empty={t("usersPage.empty")}
          testId="v2-users-table"
        />
        {total > 0 && <Pager page={Math.min(page, pages)} pages={pages} total={total} onPage={(p) => setParam("page", String(p))} />}
      </Card>

      {actions.renderDialogs()}
    </>
  );
}

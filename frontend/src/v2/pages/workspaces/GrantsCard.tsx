import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  api,
  ApiError,
  errorMessage,
  type WorkspaceGrantFilter,
  type WorkspaceGrants,
  type WorkspaceGrantUser,
} from "../../../lib/api";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, type Column, FilterSelect, Pager, SearchInput, Table, Tag } from "../../ui";

const PAGE_SIZE = 10;

/**
 * Member access to one workspace: search / filter / page are server-side and
 * live in the URL (`gq`, `granted`, `gpage`) so the view is linkable; the
 * checkbox selection is page-local and drives batch grant / revoke.
 */
export function GrantsCard({
  workspaceId,
  onTotal,
}: {
  workspaceId: string;
  /** reports `granted_total` to the summary card */
  onTotal: (total: number | null) => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [params, setParams] = useSearchParams();
  const query = params.get("gq") ?? "";
  const filterParam = params.get("granted");
  const filter: WorkspaceGrantFilter =
    filterParam === "granted" || filterParam === "ungranted" ? filterParam : "all";
  const pageNo = Math.max(1, Number(params.get("gpage") ?? "1") || 1);

  const [grants, setGrants] = useState<WorkspaceGrants | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const seq = useRef(0);

  const load = useCallback(async () => {
    const id = ++seq.current;
    setLoading(true);
    try {
      const page = await api.listWorkspaceGrants(workspaceId, {
        q: query || undefined,
        granted: filter,
        limit: PAGE_SIZE,
        offset: (pageNo - 1) * PAGE_SIZE,
      });
      if (id !== seq.current) return; // ignore out-of-order responses
      setGrants(page);
      setError(null);
      onTotal(page.granted_total);
    } catch (err) {
      if (id !== seq.current) return;
      setGrants(null);
      onTotal(null);
      // A gone workspace is expected (detached elsewhere); the empty state says so.
      setError(err instanceof ApiError && err.code === "workspace.not_found" ? null : errorMessage(err));
    } finally {
      if (id === seq.current) setLoading(false);
    }
  }, [filter, onTotal, pageNo, query, workspaceId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Selection acts on rows the operator can see: a page, search or filter
  // change starts a fresh one rather than carrying invisible ids along.
  useEffect(() => {
    setSelected(new Set());
  }, [filter, pageNo, query]);

  /** Set one grants param, keeping `view`/`id` and restarting paging. */
  const setParam = (key: string, value: string | null) => {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        if (value) next.set(key, value);
        else next.delete(key);
        if (key !== "gpage") next.delete("gpage");
        return next;
      },
      { replace: true },
    );
  };

  const rows = grants?.users ?? [];
  const pageIds = rows.map((u) => u.id);
  const allOnPage = pageIds.length > 0 && pageIds.every((id) => selected.has(id));

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const apply = async (action: "grant" | "revoke") => {
    const ids = [...selected];
    if (ids.length === 0) return;
    setBusy(true);
    try {
      const result = await api.updateWorkspaceGrants(workspaceId, { [action]: ids });
      // The count is the selection, not added/removed: re-granting a holder
      // changes no row but is what the operator asked for.
      toast(
        "success",
        t(action === "grant" ? "workspacesPage.detail.grantsGranted" : "workspacesPage.detail.grantsRevoked", {
          count: ids.length,
          total: result.granted_total,
        }),
      );
      setSelected(new Set());
      await load();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<WorkspaceGrantUser>[] = [
    {
      key: "sel",
      width: 40,
      title: (
        <input
          type="checkbox"
          checked={allOnPage}
          disabled={pageIds.length === 0}
          onChange={() => setSelected(allOnPage ? new Set() : new Set(pageIds))}
          aria-label={t("workspacesPage.detail.selectPage")}
          data-testid="v2-ws-grants-select-page"
        />
      ),
      render: (u) => (
        <input
          type="checkbox"
          checked={selected.has(u.id)}
          onChange={() => toggle(u.id)}
          aria-label={u.username}
          data-testid={`v2-ws-grant-select-${u.username}`}
        />
      ),
    },
    { key: "user", title: t("v2.workspaces.grantCol.account"), render: (u) => <b>{u.username}</b> },
    { key: "email", title: t("v2.workspaces.grantCol.email"), render: (u) => <span className="v2-muted">{u.email || "—"}</span> },
    { key: "role", title: t("v2.workspaces.grantCol.role"), render: () => <Tag tone="gray">{t("auth.roleMember")}</Tag> },
    {
      key: "status",
      title: t("v2.workspaces.grantCol.status"),
      render: (u) => <Tag tone={u.status === "active" ? "green" : "gray"}>{t(`usersPage.filters.${u.status}`)}</Tag>,
    },
    {
      key: "granted",
      title: t("v2.workspaces.grantCol.granted"),
      className: "right",
      render: (u) =>
        u.granted ? (
          <Tag tone="green" dot>
            {t("v2.workspaces.grantYes")}
          </Tag>
        ) : (
          <span className="v2-muted">{t("v2.workspaces.grantNo")}</span>
        ),
    },
  ];

  const total = grants?.total ?? 0;
  return (
    <Card
      title={t("v2.workspaces.grantsTitle")}
      sub={t("workspacesPage.detail.grantsSub", { granted: grants?.granted_total ?? 0 })}
      testId="v2-ws-grants"
    >
      <Alert>{t("workspacesPage.detail.adminHint")}</Alert>
      <div className="v2-toolbar">
        <FilterSelect
          label={t("v2.workspaces.grantFilter")}
          value={filter === "all" ? "" : filter}
          allLabel={t("v2.common.all")}
          options={[
            { value: "granted", label: t("workspacesPage.detail.grantFilters.granted") },
            { value: "ungranted", label: t("workspacesPage.detail.grantFilters.ungranted") },
          ]}
          onChange={(v) => setParam("granted", v || null)}
          testId="v2-ws-grants-filter"
        />
        <SearchInput
          value={query}
          onChange={(v) => setParam("gq", v || null)}
          placeholder={t("workspacesPage.detail.grantsSearch")}
          testId="v2-ws-grants-search"
        />
        <div className="end">
          <span className="v2-count">{t("v2.workspaces.selected", { count: selected.size })}</span>
          <Button
            kind="primary"
            disabled={selected.size === 0 || busy}
            onClick={() => void apply("grant")}
            testId="v2-ws-grants-grant"
          >
            {t("v2.workspaces.grant")}
          </Button>
          <Button
            kind="danger"
            disabled={selected.size === 0 || busy}
            onClick={() => void apply("revoke")}
            testId="v2-ws-grants-revoke"
          >
            {t("v2.workspaces.revoke")}
          </Button>
        </div>
      </div>
      <Table
        columns={columns}
        rows={rows}
        rowKey={(u) => u.id}
        loading={loading}
        error={error}
        onRetry={() => void load()}
        empty={
          query || filter !== "all" ? t("workspacesPage.detail.noMatchingAccounts") : t("workspacesPage.detail.noAccounts")
        }
        testId="v2-ws-grants-table"
      />
      <Pager
        page={pageNo}
        pages={Math.max(1, Math.ceil(total / PAGE_SIZE))}
        total={total}
        onPage={(next) => setParam("gpage", next > 1 ? String(next) : null)}
      />
    </Card>
  );
}

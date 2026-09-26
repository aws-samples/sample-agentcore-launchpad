import { Plus, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, type Workspace } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad, usePaged } from "../../hooks";
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
} from "../../ui";
import { WORKSPACE_STATUSES } from "./status";
import { ExternalTag, HubTag, StatusTag } from "./tags";

/** While any bootstrap runs, the list re-reads so its status tag moves on its own. */
const POLL_MS = 5000;

export function WorkspaceList() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const [tick, setTick] = useState(0);
  const { data, loading, error, reload } = useLoad(() => api.listWorkspaces(), `workspaces:${tick}`);
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState("");
  const [q, setQ] = useState("");

  const all = useMemo(() => data?.workspaces ?? [], [data]);
  const running = all.some((w) => w.bootstrap_status === "bootstrapping");
  useEffect(() => {
    if (!running) return;
    const timer = window.setTimeout(() => setTick((n) => n + 1), POLL_MS);
    return () => window.clearTimeout(timer);
  }, [running, data]);

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter((w) => {
      if (status && w.bootstrap_status !== status) return false;
      if (kind === "local" && w.cross_account) return false;
      if (kind === "external" && !w.cross_account) return false;
      if (!needle) return true;
      return `${w.id} ${w.name} ${w.account_id} ${w.region}`.toLowerCase().includes(needle);
    });
  }, [all, status, kind, q]);
  const paged = usePaged(rows, 12);

  const count = (s: Workspace["bootstrap_status"]) => all.filter((w) => w.bootstrap_status === s).length;
  const open = (id: string) => setParams({ view: "detail", id });

  const columns: Column<Workspace>[] = [
    {
      key: "name",
      title: t("v2.workspaces.col.name"),
      render: (w) => (
        <div className="v2-stack" style={{ gap: 2 }}>
          <span className="v2-row">
            <LinkButton onClick={() => open(w.id)} testId={`v2-ws-open-${w.id}`}>
              {w.name}
            </LinkButton>
            {w.is_default && <HubTag />}
          </span>
          <span className="sub mono">ID: {w.id}</span>
        </div>
      ),
    },
    {
      key: "account",
      title: t("v2.workspaces.col.account"),
      render: (w) => (
        <span className="v2-row">
          <span className="mono">{w.account_id}</span>
          {w.cross_account && <ExternalTag />}
        </span>
      ),
    },
    { key: "region", title: t("v2.workspaces.col.region"), render: (w) => <span className="mono">{w.region}</span> },
    { key: "status", title: t("v2.workspaces.col.status"), render: (w) => <StatusTag status={w.bootstrap_status} /> },
    { key: "created", title: t("v2.workspaces.col.created"), render: (w) => fmtTime(w.created_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (w) => (
        <div className="v2-actions">
          <LinkButton onClick={() => open(w.id)}>{t("v2.common.detail")}</LinkButton>
        </div>
      ),
    },
  ];

  return (
    <>
      <PageHeader title={t("nav.workspaces")} desc={t("v2.workspaces.desc")} />
      <div className="v2-kpis">
        <Kpi label={t("v2.workspaces.kpi.total")} value={all.length} testId="v2-ws-kpi-total" />
        <Kpi label={t("v2.workspaces.kpi.ready")} value={count("ready")} />
        <Kpi label={t("v2.workspaces.kpi.bootstrapping")} value={count("bootstrapping")} />
        <Kpi
          label={t("v2.workspaces.kpi.attention")}
          value={count("registered") + count("failed")}
          sub={t("v2.workspaces.kpi.attentionSub", { failed: count("failed") })}
          tone={count("failed") ? "bad" : undefined}
        />
      </div>
      <Card>
        <div className="v2-toolbar">
          <Button onClick={reload}>
            <RefreshCw size={14} aria-hidden="true" />
            {t("v2.common.refresh")}
          </Button>
          <Button kind="primary" onClick={() => setParams({ view: "new" })} testId="v2-ws-new">
            <Plus size={14} aria-hidden="true" />
            {t("v2.workspaces.new")}
          </Button>
          <FilterSelect
            label={t("v2.workspaces.col.status")}
            value={status}
            allLabel={t("v2.common.all")}
            options={WORKSPACE_STATUSES.map((s) => ({ value: s, label: t(`v2.workspaces.status.${s}`) }))}
            onChange={setStatus}
            testId="v2-ws-filter-status"
          />
          <FilterSelect
            label={t("v2.workspaces.kind.label")}
            value={kind}
            allLabel={t("v2.common.all")}
            options={[
              { value: "local", label: t("v2.workspaces.kind.local") },
              { value: "external", label: t("v2.workspaces.kind.external") },
            ]}
            onChange={setKind}
          />
          <div className="end">
            <SearchInput value={q} onChange={setQ} placeholder={t("v2.workspaces.search")} />
            <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
          </div>
        </div>
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(w) => w.id}
          loading={loading}
          error={error}
          onRetry={reload}
          empty={t("v2.workspaces.empty")}
          testId="v2-ws-table"
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
    </>
  );
}

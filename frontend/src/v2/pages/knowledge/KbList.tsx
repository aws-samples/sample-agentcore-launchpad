import { Plus } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { v2KnowledgeApi } from "../../../lib/api";
import type { KnowledgeBaseSummary } from "../../../lib/knowledgeBases";
import { fmtTime } from "../../format";
import { useLoad, usePaged } from "../../hooks";
import {
  Alert,
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
import { KbStatusTag } from "./common";
import { KB_STATUSES, kbStatusLabel } from "./status";
import { useKbDelete } from "./useKbDelete";

const TRANSIENT = new Set(["CREATING", "DELETING"]);

export function KbList({ staleId, onDismissStale }: { staleId: string | null; onDismissStale: () => void }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const { data, loading, error, reload } = useLoad(() => v2KnowledgeApi.list(), "kbs");
  const del = useKbDelete(() => reload());
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");

  const kbs = useMemo(() => data?.items ?? [], [data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return kbs.filter((kb) => {
      if (status && kb.status !== status) return false;
      return !needle || `${kb.name} ${kb.kb_id} ${kb.description}`.toLowerCase().includes(needle);
    });
  }, [kbs, status, q]);
  const paged = usePaged(rows, 12);
  const count = (s: string) => kbs.filter((kb) => kb.status === s).length;
  const agentCount = new Set(kbs.flatMap((kb) => kb.attached_agents)).size;

  // a KB provisioning / deleting moves on its own: refresh until it settles
  const transient = kbs.some((kb) => TRANSIENT.has(kb.status));
  useEffect(() => {
    if (!transient) return;
    const timer = window.setInterval(reload, 8000);
    return () => window.clearInterval(timer);
  }, [transient, reload]);

  const open = (kb: KnowledgeBaseSummary) => setParams({ view: "detail", id: kb.kb_id });

  const columns: Column<KnowledgeBaseSummary>[] = [
    {
      key: "name",
      title: t("knowledge.cols.name"),
      render: (kb) => (
        <>
          <LinkButton onClick={() => open(kb)} testId={`v2-kb-${kb.name}`}>
            {kb.name}
          </LinkButton>
          <span className="sub mono">ID: {kb.kb_id}</span>
        </>
      ),
    },
    {
      key: "desc",
      title: t("knowledge.create.description"),
      render: (kb) =>
        kb.description ? (
          <span className="v2-knowledge-clamp" title={kb.description}>
            {kb.description}
          </span>
        ) : (
          <span className="v2-muted">—</span>
        ),
    },
    { key: "status", title: t("knowledge.cols.status"), render: (kb) => <KbStatusTag status={kb.status} /> },
    {
      key: "ds",
      title: t("knowledge.cols.dataSources"),
      render: (kb) => <span className="mono">{kb.data_source_count}</span>,
    },
    {
      key: "agents",
      title: t("knowledge.cols.agents"),
      render: (kb) =>
        kb.attached_agents.length ? (
          <span className="v2-knowledge-agents" title={kb.attached_agents.join(", ")}>
            <span className="mono">{kb.attached_agents.length}</span>
            <span className="sub">{kb.attached_agents.join(", ")}</span>
          </span>
        ) : (
          <span className="mono v2-muted">0</span>
        ),
    },
    { key: "updated", title: t("knowledge.cols.updated"), className: "nowrap", render: (kb) => fmtTime(kb.updated_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (kb) => (
        <div className="v2-actions">
          <LinkButton onClick={() => open(kb)}>{t("v2.common.view")}</LinkButton>
          <LinkButton
            onClick={() => setParams({ view: "detail", id: kb.kb_id, tab: "retrieve" })}
            disabled={kb.status !== "ACTIVE"}
          >
            {t("v2.knowledge.retrieveTest")}
          </LinkButton>
          <LinkButton
            danger
            disabled={kb.status === "DELETING"}
            onClick={() => del.ask(kb)}
            testId={`v2-kb-delete-${kb.name}`}
          >
            {t("v2.common.delete")}
          </LinkButton>
        </div>
      ),
    },
  ];

  return (
    <>
      <PageHeader title={t("knowledge.title")} desc={t("knowledge.list.panelSub")} />
      {staleId !== null && (
        <Alert tone="warn" action={<LinkButton onClick={onDismissStale}>{t("staleLink.dismiss")}</LinkButton>}>
          {staleId
            ? t("staleLink.body", { kind: t("staleLink.kind.knowledgeBase"), id: staleId })
            : t("staleLink.bodyMissing", { kind: t("staleLink.kind.knowledgeBase") })}
        </Alert>
      )}
      <div className="v2-kpis">
        <Kpi label={t("v2.knowledge.kpiTotal")} value={data ? kbs.length : "—"} testId="v2-kb-kpi-total" />
        <Kpi label={t("v2.knowledge.kpiActive")} value={data ? count("ACTIVE") : "—"} tone="good" />
        <Kpi
          label={t("v2.knowledge.kpiTransient")}
          value={data ? count("CREATING") + count("DELETING") : "—"}
        />
        <Kpi
          label={t("v2.knowledge.kpiAgents")}
          value={data ? agentCount : "—"}
          sub={data && count("FAILED") ? t("v2.knowledge.kpiFailed", { count: count("FAILED") }) : undefined}
        />
      </div>
      <Card>
        <div className="v2-toolbar">
          <Button onClick={reload}>{t("v2.common.refresh")}</Button>
          <Button kind="primary" onClick={() => setParams({ view: "new" })} testId="v2-kb-new">
            <Plus size={14} aria-hidden="true" />
            {t("knowledge.create.cta")}
          </Button>
          <FilterSelect
            label={t("knowledge.cols.status")}
            value={status}
            allLabel={t("v2.common.all")}
            onChange={setStatus}
            options={KB_STATUSES.map((s) => ({ value: s, label: kbStatusLabel(t, s) }))}
          />
          <div className="end">
            <SearchInput value={q} onChange={setQ} placeholder={t("v2.knowledge.search")} testId="v2-kb-search" />
            <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
          </div>
        </div>
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(kb) => kb.kb_id}
          loading={loading}
          error={error}
          onRetry={reload}
          empty={
            kbs.length ? (
              t("v2.knowledge.noMatch")
            ) : (
              <>
                <b>{t("knowledge.empty.title")}</b>
                <div className="v2-knowledge-empty">{t("knowledge.empty.body")}</div>
              </>
            )
          }
          testId="v2-kb-table"
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
      {del.dialog}
    </>
  );
}

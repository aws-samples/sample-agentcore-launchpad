import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, ApiError, type AssistantConversationSummary, type AssistantStatus } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
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
import { PROPOSAL_TONE, useApiMessage } from "./common";
import { AdlcCard } from "./Progress";
import { useClearConversation } from "./useClearConversation";

const PROPOSAL_STATUSES = ["draft", "invalid", "approved", "rejected", "superseded"] as const;

/** The conversation history of this workspace (the classic side list), with NEW. */
export function AssistantList({
  status, workspaceId, onUnavailable,
}: {
  status: AssistantStatus;
  workspaceId: string;
  onUnavailable: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const apiMessage = useApiMessage();
  const [, setParams] = useSearchParams();
  const { data, loading, error, reload } = useLoad(() => api.assistantConversations(), `conversations:${workspaceId}`);
  const clear = useClearConversation(() => reload());
  const [creating, setCreating] = useState(false);
  const [proposal, setProposal] = useState("");
  const [q, setQ] = useState("");

  const conversations = useMemo(() => data?.conversations ?? [], [data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return conversations.filter((c) => {
      if (proposal === "none" ? c.proposal_status !== null : proposal && c.proposal_status !== proposal) return false;
      return !needle || `${c.title} ${c.id}`.toLowerCase().includes(needle);
    });
  }, [conversations, proposal, q]);
  const paged = usePaged(rows, 12);
  const count = (s: string) => conversations.filter((c) => c.proposal_status === s).length;
  const open = (id: string) => setParams({ view: "detail", id });

  const create = async () => {
    setCreating(true);
    try {
      const detail = await api.assistantCreateConversation();
      if (detail.catalog.warnings.length) {
        toast("error", t("assistantPage.catalogWarnings", { list: detail.catalog.warnings.join("; ") }));
      }
      open(detail.id);
    } catch (err) {
      toast("error", apiMessage(err));
      if (err instanceof ApiError && err.code === "assistant.unavailable") onUnavailable();
    } finally {
      setCreating(false);
    }
  };

  const columns: Column<AssistantConversationSummary>[] = [
    {
      key: "title",
      title: t("v2.assistant.colTitle"),
      render: (c) => (
        <>
          <LinkButton onClick={() => open(c.id)} title={c.title || undefined} testId={`v2-assistant-open-${c.id}`}>
            <span className="v2-assistant-title">{c.title || c.id.slice(0, 8)}</span>
          </LinkButton>
          <span className="sub mono">ID: {c.id} · {t("v2.common.createdAt")} {fmtTime(c.created_at)}</span>
        </>
      ),
    },
    {
      key: "proposal",
      title: t("v2.assistant.colProposal"),
      render: (c) =>
        c.proposal_status ? (
          <span className="v2-row" style={{ flexWrap: "nowrap" }}>
            <Tag tone={PROPOSAL_TONE[c.proposal_status]} dot>
              {t(`assistantPage.status.${c.proposal_status}`)}
            </Tag>
            <span className="v2-muted mono">r{c.proposal_revision}</span>
          </span>
        ) : (
          <span className="v2-muted">{t("v2.assistant.noProposal")}</span>
        ),
    },
    {
      key: "turns",
      title: t("v2.assistant.colTurns"),
      render: (c) => (
        <span className="v2-row">
          <span className="mono">{c.turns}</span>
          {c.turn_in_progress !== null && <Tag tone="blue">{t("assistantPage.streaming")}</Tag>}
        </span>
      ),
    },
    { key: "updated", title: t("v2.assistant.colUpdated"), className: "nowrap", render: (c) => fmtTime(c.updated_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (c) => (
        <div className="v2-actions">
          <LinkButton onClick={() => open(c.id)}>{t("v2.assistant.continue")}</LinkButton>
          <LinkButton
            danger
            disabled={clear.busy}
            title={t("assistantPage.clear.action")}
            onClick={() => void clear.ask(c)}
            testId={`v2-assistant-clear-${c.id}`}
          >
            {t("v2.assistant.clear")}
          </LinkButton>
        </div>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        title={t("nav.assistant")}
        desc={t("assistantPage.description")}
        end={
          <span className="v2-muted mono" style={{ fontSize: 12.5 }}>
            {t("assistantPage.meta", { label: status.preset.label, workspace: status.workspace_id, region: status.region })}
          </span>
        }
      />
      <div className="v2-kpis">
        <Kpi label={t("v2.assistant.kpiTotal")} value={data ? conversations.length : "—"} testId="v2-assistant-kpi-total" />
        <Kpi label={t("v2.assistant.kpiDraft")} value={data ? count("draft") : "—"} />
        <Kpi label={t("v2.assistant.kpiApproved")} value={data ? count("approved") : "—"} tone="good" />
        <Kpi
          label={t("v2.assistant.kpiInvalid")}
          value={data ? count("invalid") : "—"}
          tone={count("invalid") ? "bad" : undefined}
        />
      </div>
      <AdlcCard open={!loading && conversations.length === 0} />
      <Card title={t("assistantPage.historyTitle")} sub={t("assistantPage.historySub", { n: conversations.length })}>
        <div className="v2-toolbar">
          <Button onClick={reload}>{t("v2.common.refresh")}</Button>
          <Button kind="primary" disabled={creating} onClick={() => void create()} testId="v2-assistant-new">
            <Plus size={14} aria-hidden="true" />
            {t("assistantPage.newConversation")}
          </Button>
          <FilterSelect
            label={t("v2.assistant.colProposal")}
            value={proposal}
            allLabel={t("v2.common.all")}
            onChange={setProposal}
            options={[
              { value: "none", label: t("v2.assistant.noProposal") },
              ...PROPOSAL_STATUSES.map((s) => ({ value: s, label: t(`assistantPage.status.${s}`) })),
            ]}
          />
          <div className="end">
            <SearchInput value={q} onChange={setQ} placeholder={t("v2.assistant.search")} testId="v2-assistant-search" />
            <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
          </div>
        </div>
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(c) => c.id}
          loading={loading}
          error={error}
          onRetry={reload}
          empty={conversations.length ? t("v2.assistant.noMatch") : t("assistantPage.historyEmpty")}
          testId="v2-assistant-table"
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
      {clear.dialog}
    </>
  );
}

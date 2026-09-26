import { Copy } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, type ObsSessionRow, type V2Range } from "../../../lib/api";
import { fmtNumber, fmtTime } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
import { Card, type Column, FilterSelect, LinkButton, Pager, SearchInput, Table, Tag } from "../../ui";
import { approxCost, copyText, shortId, useReportCache } from "./common";

export function CopyId({ id }: { id: string }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  return (
    <button
      type="button"
      className="v2-observability-copy"
      title={t("v2.observability.copyId")}
      aria-label={t("v2.observability.copyId")}
      onClick={(e) => {
        e.stopPropagation();
        copyText(id).then(
          () => toast("success", t("v2.observability.copied")),
          () => toast("error", t("v2.observability.copyFailed")),
        );
      }}
    >
      <Copy size={12} aria-hidden="true" />
    </button>
  );
}

export function SessionList({
  range,
  force,
  onCacheHint,
}: {
  range: V2Range;
  force: number;
  onCacheHint: (hint: string | null) => void;
}) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const { data, loading, error, reload } = useLoad(() => api.obsSessions(range, force > 0), `obs-sessions:${range}:${force}`);
  useReportCache(data?.cache.age_seconds, data, loading, onCacheHint);
  const [agent, setAgent] = useState("");
  const [errors, setErrors] = useState("");
  const [q, setQ] = useState("");

  const sessions = useMemo(() => data?.sessions ?? [], [data]);
  const agents = useMemo(() => [...new Set(sessions.map((r) => r.agent).filter(Boolean))].sort(), [sessions]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return sessions.filter((r) => {
      if (agent && r.agent !== agent) return false;
      if (errors === "errors" && r.errors === 0) return false;
      if (errors === "clean" && r.errors > 0) return false;
      return !needle || r.session_id.toLowerCase().includes(needle);
    });
  }, [sessions, agent, errors, q]);
  const paged = usePaged(rows, 12);

  const open = (row: ObsSessionRow) =>
    setParams({ tab: "sessions", view: "session", id: row.session_id, ...(range !== "24h" ? { range } : {}) });

  const columns: Column<ObsSessionRow>[] = [
    {
      key: "session",
      title: t("v2.observability.col.session"),
      render: (row) => (
        <span className="v2-observability-idcell">
          <LinkButton onClick={() => open(row)} title={row.session_id}>
            <span className="mono">{shortId(row.session_id, 22)}</span>
          </LinkButton>
          <CopyId id={row.session_id} />
        </span>
      ),
    },
    { key: "agent", title: "Agent", render: (row) => row.agent || "—" },
    { key: "traces", title: t("v2.observability.col.traces"), className: "num", render: (row) => row.traces },
    { key: "llm", title: t("v2.observability.col.llmCalls"), className: "num", render: (row) => row.llm_calls },
    {
      key: "tokens",
      title: t("v2.observability.col.tokensCost"),
      className: "num nowrap",
      render: (row) =>
        row.tokens.total > 0 ? (
          <>
            {fmtNumber(row.tokens.total)}
            <span className="sub">{approxCost(row.est_cost_usd)}</span>
          </>
        ) : (
          "—"
        ),
    },
    {
      key: "time",
      title: t("v2.observability.col.activity"),
      className: "nowrap",
      render: (row) => (
        <>
          {fmtTime(row.last)}
          <span className="sub">{t("v2.observability.firstAt", { time: fmtTime(row.first) })}</span>
        </>
      ),
    },
    {
      key: "errors",
      title: t("v2.observability.col.errors"),
      render: (row) => (row.errors > 0 ? <Tag tone="red">{row.errors}</Tag> : <Tag tone="green">0</Tag>),
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (row) => <LinkButton onClick={() => open(row)}>{t("v2.common.view")}</LinkButton>,
    },
  ];

  return (
    <Card>
      <div className="v2-toolbar">
        <FilterSelect
          label="Agent"
          value={agent}
          allLabel={t("v2.common.all")}
          onChange={setAgent}
          options={agents.map((a) => ({ value: a, label: a }))}
          testId="v2-obs-sessions-agent"
        />
        <FilterSelect
          label={t("v2.observability.col.errors")}
          value={errors}
          allLabel={t("v2.common.all")}
          onChange={setErrors}
          options={[
            { value: "errors", label: t("v2.observability.errorsOnly") },
            { value: "clean", label: t("v2.observability.noErrors") },
          ]}
          testId="v2-obs-sessions-errors"
        />
        <div className="end">
          <SearchInput value={q} onChange={setQ} placeholder={t("v2.observability.searchSession")} />
          <span className="v2-count">{t("v2.observability.scanned", { count: rows.length, range: range.toUpperCase() })}</span>
        </div>
      </div>
      <Table
        columns={columns}
        rows={paged.slice}
        rowKey={(row) => row.session_id}
        loading={loading}
        error={error}
        onRetry={reload}
        empty={sessions.length === 0 ? t("v2.observability.sessionsEmpty") : t("v2.observability.noMatch")}
        testId="v2-obs-sessions-table"
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
    </Card>
  );
}

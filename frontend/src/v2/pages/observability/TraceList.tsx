import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, type ObsTraceRow, type V2Range } from "../../../lib/api";
import { fmtDuration, fmtNumber, fmtTime } from "../../format";
import { useLoad, usePaged } from "../../hooks";
import { Card, type Column, FilterSelect, LinkButton, Pager, SearchInput, Table, Tag } from "../../ui";
import { approxCost, isSystemTrace, shortId, TIMEOUT_MS, useReportCache } from "./common";

export function TraceStatusTag({ status, durationMs }: { status: "ok" | "error"; durationMs: number }) {
  const { t } = useTranslation();
  if (status === "ok") return <Tag tone="green">{t("v2.observability.status.ok")}</Tag>;
  return <Tag tone="red">{durationMs > TIMEOUT_MS ? t("v2.observability.status.timeout") : t("v2.observability.status.error")}</Tag>;
}

export function TraceList({
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
  const { data, loading, error, reload } = useLoad(() => api.obsTraces(range, force > 0), `obs-traces:${range}:${force}`);
  useReportCache(data?.cache.age_seconds, data, loading, onCacheHint);
  const [agent, setAgent] = useState("");
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [showSystem, setShowSystem] = useState(false);

  const traces = useMemo(() => data?.traces ?? [], [data]);
  const agents = useMemo(() => [...new Set(traces.map((r) => r.agent).filter(Boolean))].sort(), [traces]);
  const systemCount = useMemo(() => traces.filter(isSystemTrace).length, [traces]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return traces.filter((r) => {
      if (!showSystem && isSystemTrace(r)) return false;
      if (agent && r.agent !== agent) return false;
      if (status && r.status !== status) return false;
      return !needle || `${r.trace_id} ${r.session_id ?? ""}`.toLowerCase().includes(needle);
    });
  }, [traces, agent, status, q, showSystem]);
  const paged = usePaged(rows, 12);

  const rangeParam: Record<string, string> = range !== "24h" ? { range } : {};
  const openTrace = (row: ObsTraceRow) => setParams({ tab: "traces", view: "trace", id: row.trace_id, ...rangeParam });
  const openSession = (sessionId: string) => setParams({ tab: "sessions", view: "session", id: sessionId, ...rangeParam });

  const columns: Column<ObsTraceRow>[] = [
    {
      key: "trace",
      title: t("v2.observability.col.trace"),
      render: (row) => (
        <>
          <LinkButton onClick={() => openTrace(row)} title={row.trace_id}>
            <span className="mono">{shortId(row.trace_id, 16)}</span>
          </LinkButton>
          <span className="sub">
            {fmtTime(row.time)} · {row.root_operation}
          </span>
        </>
      ),
    },
    { key: "agent", title: "Agent", render: (row) => row.agent || "—" },
    {
      key: "session",
      title: t("v2.observability.col.session"),
      render: (row) =>
        row.session_id ? (
          <LinkButton onClick={() => openSession(row.session_id as string)} title={row.session_id}>
            <span className="mono">{shortId(row.session_id, 14)}</span>
          </LinkButton>
        ) : (
          "—"
        ),
    },
    { key: "dur", title: t("v2.observability.col.duration"), className: "num", render: (row) => fmtDuration(row.duration_ms) },
    {
      key: "spans",
      title: t("v2.observability.col.spansLlm"),
      className: "num",
      render: (row) => (
        <span title={row.model ?? undefined}>
          {row.span_count} / {row.llm_count}
        </span>
      ),
    },
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
      key: "status",
      title: t("v2.observability.col.status"),
      render: (row) => <TraceStatusTag status={row.status} durationMs={row.duration_ms} />,
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (row) => <LinkButton onClick={() => openTrace(row)}>{t("v2.observability.waterfall")}</LinkButton>,
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
          testId="v2-obs-traces-agent"
        />
        <FilterSelect
          label={t("v2.observability.col.status")}
          value={status}
          allLabel={t("v2.common.all")}
          onChange={setStatus}
          options={[
            { value: "ok", label: t("v2.observability.status.ok") },
            { value: "error", label: t("v2.observability.status.error") },
          ]}
          testId="v2-obs-traces-status"
        />
        {systemCount > 0 && (
          <label className="v2-check" title={t("v2.observability.systemHint")}>
            <input
              type="checkbox"
              checked={showSystem}
              onChange={(e) => setShowSystem(e.target.checked)}
              data-testid="v2-obs-traces-system"
            />
            {t("v2.observability.showSystem", { count: systemCount })}
          </label>
        )}
        <div className="end">
          <SearchInput value={q} onChange={setQ} placeholder={t("v2.observability.searchTrace")} />
          <span className="v2-count">{t("v2.observability.scannedTraces", { count: rows.length, range: range.toUpperCase() })}</span>
        </div>
      </div>
      <Table
        columns={columns}
        rows={paged.slice}
        rowKey={(row) => row.trace_id}
        loading={loading}
        error={error}
        onRetry={reload}
        empty={traces.length === 0 ? t("v2.observability.tracesEmpty") : t("v2.observability.noMatch")}
        testId="v2-obs-traces-table"
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
    </Card>
  );
}

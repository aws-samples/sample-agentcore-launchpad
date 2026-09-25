import { DatabaseZap } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, type ObsSpanNode, type ObsTraceRow, type V2Range } from "../../../lib/api";
import { fmtDuration, fmtNumber, fmtTime, RANGES, rangeLabel } from "../../format";
import { useLoad, usePaged } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Descriptions,
  FilterSelect,
  FlowHeader,
  LinkButton,
  Pager,
  SearchInput,
  Spin,
  Table,
  Tag,
} from "../../ui";
import { AddToDatasetModal } from "./AddToDataset";

function asRange(value: string | null): V2Range {
  return (RANGES as string[]).includes(value ?? "") ? (value as V2Range) : "24h";
}

// ─── list ──────────────────────────────────────────────────────────────────
export function TracesTab() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const range = asRange(params.get("range"));
  const [force, setForce] = useState(0);
  const { data, loading, error, reload } = useLoad(() => api.obsTraces(range, force > 0), `traces:${range}:${force}`);
  const [agent, setAgent] = useState("");
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [adding, setAdding] = useState(false);

  const traces = useMemo(() => data?.traces ?? [], [data]);
  const agents = useMemo(() => [...new Set(traces.map((r) => r.agent).filter(Boolean))].sort(), [traces]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return traces.filter((r) => {
      if (agent && r.agent !== agent) return false;
      if (status && r.status !== status) return false;
      if (!needle) return true;
      return `${r.trace_id} ${r.session_id ?? ""} ${r.agent} ${r.root_operation}`.toLowerCase().includes(needle);
    });
  }, [traces, agent, status, q]);
  const paged = usePaged(rows, 15);

  const selectedSessions = useMemo(() => {
    const ids = traces.filter((r) => selected.has(r.trace_id) && r.session_id).map((r) => r.session_id as string);
    return [...new Set(ids)];
  }, [traces, selected]);

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const pageIds = paged.slice.filter((r) => r.session_id).map((r) => r.trace_id);
  const allOnPage = pageIds.length > 0 && pageIds.every((id) => selected.has(id));

  const open = (row: ObsTraceRow) => setParams({ tab: "traces", view: "trace", id: row.trace_id, range });

  const columns: Column<ObsTraceRow>[] = [
    {
      key: "sel",
      title: (
        <input
          type="checkbox"
          aria-label={t("v2.traces.selectPage")}
          checked={allOnPage}
          onChange={() =>
            setSelected((prev) => {
              const next = new Set(prev);
              for (const id of pageIds) {
                if (allOnPage) next.delete(id);
                else next.add(id);
              }
              return next;
            })
          }
        />
      ),
      width: 36,
      render: (row) => (
        <input
          type="checkbox"
          aria-label={row.trace_id}
          disabled={!row.session_id}
          title={row.session_id ? undefined : t("v2.traces.noSession")}
          checked={selected.has(row.trace_id)}
          onChange={() => toggle(row.trace_id)}
        />
      ),
    },
    {
      key: "trace",
      title: t("v2.traces.colTrace"),
      render: (row) => (
        <>
          <LinkButton onClick={() => open(row)}>
            <span className="mono">{row.trace_id.slice(0, 12)}…</span>
          </LinkButton>
          <span className="sub">{row.root_operation}</span>
        </>
      ),
    },
    { key: "agent", title: "Agent", render: (row) => row.agent || "—" },
    {
      key: "session",
      title: t("v2.traces.colSession"),
      render: (row) => (row.session_id ? <span className="mono" title={row.session_id}>{row.session_id.slice(0, 14)}…</span> : "—"),
    },
    { key: "tokens", title: "Tokens", className: "num", render: (row) => fmtNumber(row.tokens.total) },
    { key: "dur", title: t("v2.traces.colDuration"), className: "num", render: (row) => fmtDuration(row.duration_ms) },
    { key: "steps", title: "Steps", className: "num", render: (row) => row.span_count },
    {
      key: "model",
      title: t("v2.traces.colModel"),
      render: (row) => (row.model ? `${row.model}${row.llm_count ? ` ×${row.llm_count}` : ""}` : "—"),
    },
    {
      key: "status",
      title: t("v2.traces.colStatus"),
      render: (row) =>
        row.status === "ok" ? (
          <Tag tone="green">{t("v2.traces.ok")}</Tag>
        ) : (
          <Tag tone="red">{t("v2.traces.error", { count: row.error_count })}</Tag>
        ),
    },
    { key: "time", title: t("v2.traces.colStart"), className: "nowrap", render: (row) => fmtTime(row.time) },
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
          label={t("v2.common.timeRange")}
          value={range}
          onChange={(v) => setParams({ tab: "traces", range: v })}
          options={RANGES.map((r) => ({ value: r, label: rangeLabel(t, r) }))}
        />
        <FilterSelect
          label="Agent"
          value={agent}
          allLabel={t("v2.common.all")}
          onChange={setAgent}
          options={agents.map((a) => ({ value: a, label: a }))}
        />
        <FilterSelect
          label={t("v2.traces.colStatus")}
          value={status}
          allLabel={t("v2.common.all")}
          onChange={setStatus}
          options={[
            { value: "ok", label: t("v2.traces.ok") },
            { value: "error", label: t("v2.traces.errorShort") },
          ]}
        />
        <Button onClick={() => setForce((n) => n + 1)}>{t("v2.common.refresh")}</Button>
        <Button kind="primary" disabled={selectedSessions.length === 0} onClick={() => setAdding(true)} testId="v2-traces-add">
          <DatabaseZap size={14} aria-hidden="true" />
          {t("v2.traces.addToDataset", { count: selectedSessions.length })}
        </Button>
        <div className="end">
          <SearchInput value={q} onChange={setQ} placeholder={t("v2.traces.search")} />
          <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
        </div>
      </div>
      <Table
        columns={columns}
        rows={paged.slice}
        rowKey={(row) => row.trace_id}
        loading={loading}
        error={error}
        onRetry={reload}
        empty={t("v2.traces.empty")}
        testId="v2-traces-table"
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      <AddToDatasetModal
        open={adding}
        sessionIds={selectedSessions}
        range={range}
        onClose={() => setAdding(false)}
        onDone={() => setSelected(new Set())}
      />
    </Card>
  );
}

// ─── detail ────────────────────────────────────────────────────────────────
function flatten(nodes: ObsSpanNode[], out: ObsSpanNode[] = []): ObsSpanNode[] {
  for (const node of nodes) {
    out.push(node);
    flatten(node.children, out);
  }
  return out;
}

const CATEGORY_TONE = {
  llm: "blue",
  tool: "orange",
  memory: "green",
  gateway: "gray",
  http: "gray",
  agent: "blue",
  other: "gray",
} as const;

export function TraceDetail({ traceId }: { traceId: string }) {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const range = asRange(params.get("range"));
  const trace = useLoad(() => api.obsTrace(traceId, range), `trace:${traceId}:${range}`);
  const sessionId = trace.data?.meta.session_id ?? null;
  const session = useLoad(
    () => (sessionId ? api.obsSession(sessionId, range) : Promise.resolve(null)),
    `trace-session:${sessionId ?? "none"}:${range}`,
  );
  const [adding, setAdding] = useState(false);
  const [raw, setRaw] = useState(false);

  const back = () => setParams({ tab: "traces", range });
  const meta = trace.data?.meta;
  const spans = trace.data ? flatten(trace.data.tree) : [];
  const turns = session.data?.transcript.turns ?? [];

  return (
    <>
      <FlowHeader
        title={t("v2.traces.detailTitle", { id: traceId.slice(0, 12) })}
        onBack={back}
        end={
          <Button kind="primary" disabled={!sessionId} onClick={() => setAdding(true)} testId="v2-trace-add">
            {t("v2.traces.addOne")}
          </Button>
        }
      />
      {trace.loading ? (
        <Spin />
      ) : trace.error ? (
        <Alert tone="error">{trace.error}</Alert>
      ) : meta ? (
        <>
          <Card title={t("v2.traces.sessionInfo")}>
            <Descriptions
              items={[
                { label: "Agent", value: meta.agent || "—" },
                { label: t("v2.traces.colSession"), value: meta.session_id ? <span className="mono">{meta.session_id}</span> : "—" },
                { label: "Trace ID", value: <span className="mono">{traceId}</span> },
                { label: t("v2.traces.colStart"), value: fmtTime(meta.start) },
                { label: t("v2.traces.rootOp"), value: meta.root_operation ?? "—" },
                {
                  label: t("v2.traces.colStatus"),
                  value: meta.status === "ok" ? <Tag tone="green">{t("v2.traces.ok")}</Tag> : <Tag tone="red">{t("v2.traces.errorShort")}</Tag>,
                },
              ]}
            />
          </Card>
          <Card title={t("v2.traces.metrics")}>
            <Descriptions
              items={[
                { label: "Steps", value: meta.span_count },
                { label: t("v2.traces.llmCalls"), value: meta.llm_count },
                {
                  label: "Tokens",
                  value: t("v2.traces.tokensDetail", {
                    total: fmtNumber(meta.tokens.total),
                    input: fmtNumber(meta.tokens.input),
                    output: fmtNumber(meta.tokens.output),
                  }),
                },
                { label: t("v2.traces.colDuration"), value: fmtDuration(meta.duration_ms) },
                { label: t("v2.traces.cost"), value: meta.est_cost_usd == null ? "—" : `$${meta.est_cost_usd.toFixed(4)}` },
                { label: t("v2.traces.service"), value: meta.service ?? "—" },
              ]}
            />
          </Card>
          <Card title={t("v2.traces.conversation")} sub={sessionId ? undefined : t("v2.traces.noSession")}>
            {session.loading && sessionId ? (
              <Spin />
            ) : turns.length === 0 ? (
              <p className="v2-muted">
                {session.data?.transcript.available === false
                  ? t("v2.traces.noTranscript")
                  : t("v2.traces.noTurns")}
              </p>
            ) : (
              turns.map((turn, i) => (
                <div key={i} className={turn.role === "user" ? "v2-turn user" : "v2-turn"}>
                  <span className="who">{turn.role === "user" ? t("v2.traces.user") : "Agent"}</span>
                  <div className="msg">{turn.text}</div>
                </div>
              ))
            )}
          </Card>
          <Card title={t("v2.traces.steps")} sub={t("v2.traces.stepsSub", { count: spans.length })}>
            {spans.map((span) => (
              <div key={span.span_id ?? `${span.name}:${span.start_offset_ms}`} className="v2-span">
                <Tag tone={CATEGORY_TONE[span.category]}>{t(`v2.spanCategory.${span.category}`)}</Tag>
                <span className="name" style={{ paddingLeft: span.depth * 14 }} title={span.name}>
                  {span.tool_name ?? span.name}
                  {span.model && <span className="v2-muted"> · {span.model}</span>}
                </span>
                {span.status !== "ok" && span.status !== "UNSET" && <Tag tone="red">{span.status}</Tag>}
                <span className="track" aria-hidden="true">
                  <span style={{ left: `${span.offset_pct}%`, width: `${Math.max(span.width_pct, 0.5)}%` }} />
                </span>
                <span className="dur">{fmtDuration(span.duration_ms)}</span>
              </div>
            ))}
          </Card>
          <Card
            title={t("v2.traces.raw")}
            end={<LinkButton onClick={() => setRaw((v) => !v)}>{raw ? t("v2.common.collapse") : t("v2.common.expand")}</LinkButton>}
          >
            {raw ? (
              <pre className="v2-pre">{JSON.stringify({ meta, spans: trace.data?.spans }, null, 2)}</pre>
            ) : (
              <p className="v2-muted">{t("v2.traces.rawHint")}</p>
            )}
          </Card>
        </>
      ) : null}
      <AddToDatasetModal
        open={adding && sessionId !== null}
        sessionIds={sessionId ? [sessionId] : []}
        range={range}
        onClose={() => setAdding(false)}
      />
    </>
  );
}

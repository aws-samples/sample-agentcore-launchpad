import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import type { V2LogStream, V2LogStreams } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad, usePaged } from "../../hooks";
import { Alert, Button, Pager, SearchInput, Table, Tag } from "../../ui";

/** Wait this long after the last keystroke before searching (content search reads CloudWatch). */
const DEBOUNCE_MS = 600;

/**
 * 日志 data source of the task wizard: the streams of the agent's runtime log
 * group in the chosen window, narrowed by a keyword (stream name or log
 * content). A row is one session — its own `runtime-logs-<sessionId>` stream,
 * or (Harness) its slice of the shared `otel-rt-logs`; the picked session ids
 * become the run's `session_ids` scope. Streams no session owns are hidden
 * unless asked for, and never selectable. `load` reads the rows — an agent's
 * runtime log group, or (no platform agent) the sessions of a CloudWatch
 * source; `loadKey` names that source so a change refetches.
 */
export function LogStreamPicker({
  load,
  loadKey,
  hint,
  hours,
  selected,
  onChange,
}: {
  load: (hours: number, q?: string) => Promise<V2LogStreams>;
  loadKey: string;
  hint: string;
  hours: number;
  selected: string[];
  onChange: (sessionIds: string[]) => void;
}) {
  const { t } = useTranslation();
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");
  const [showShared, setShowShared] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setQ(input.trim()), DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [input]);
  const { data, loading, error, reload } = useLoad(
    () => load(hours, q || undefined),
    `log-streams:${loadKey}:${hours}:${q}`,
  );
  const all = data?.streams ?? [];
  const shared = all.filter((r) => r.kind === "shared").length;
  const rows = showShared ? all : all.filter((r) => r.kind !== "shared");
  const paged = usePaged(rows, 10);
  const pickable = rows.flatMap((r) => (r.session_id ? [r.session_id] : []));
  const toggle = (sid: string) => onChange(selected.includes(sid) ? selected.filter((s) => s !== sid) : [...selected, sid]);

  return (
    <>
      <div className="v2-toolbar">
        <SearchInput value={input} onChange={setInput} placeholder={t("v2.tasks.logs.keywordPlaceholder")} testId="v2-task-logs-q" />
        <Button onClick={reload}>{t("v2.common.refresh")}</Button>
        <Button disabled={pickable.length === 0} onClick={() => onChange([...new Set([...selected, ...pickable])])} testId="v2-task-logs-all">
          {t("v2.tasks.logs.selectAll", { count: pickable.length })}
        </Button>
        <Button disabled={selected.length === 0} onClick={() => onChange([])}>
          {t("v2.tasks.logs.selectNone")}
        </Button>
        {shared > 0 && (
          <label className="v2-check">
            <input type="checkbox" checked={showShared} onChange={() => setShowShared(!showShared)} data-testid="v2-task-logs-shared" />
            {t("v2.tasks.logs.showShared", { count: shared })}
          </label>
        )}
        <div className="end">
          <span className="v2-count">{t("v2.tasks.logs.picked", { count: selected.length })}</span>
        </div>
      </div>
      <div className="v2-muted" style={{ fontSize: 13, margin: "0 0 12px", display: "flex", flexDirection: "column", gap: 2 }}>
        <span>{hint}</span>
        {data && (
          <span>
            {q ? t("v2.tasks.logs.foundQ", { count: rows.length, q, hours }) : t("v2.tasks.logs.found", { count: rows.length, hours })}
            {" · "}
            {t("v2.tasks.logs.logGroup")} <span className="mono">{data.log_group}</span>
          </span>
        )}
      </div>
      {data?.truncated && (
        <div style={{ marginBottom: 12 }}>
          <Alert tone="warn">{t("v2.tasks.logs.truncated")}</Alert>
        </div>
      )}
      <Table
        columns={[
          {
            key: "sel",
            title: "",
            width: 36,
            render: (r: V2LogStream) =>
              r.session_id ? (
                <input
                  type="checkbox"
                  aria-label={r.stream}
                  checked={selected.includes(r.session_id)}
                  onChange={() => toggle(r.session_id as string)}
                  data-testid={`v2-task-log-${r.session_id}`}
                />
              ) : (
                <input type="checkbox" disabled aria-label={r.stream} title={t("v2.tasks.logs.shared")} />
              ),
          },
          {
            key: "stream",
            title: t("v2.tasks.logs.colStream"),
            render: (r: V2LogStream) => (
              <>
                <span className="v2-row" style={{ flexWrap: "nowrap", gap: 6 }}>
                  <span className="mono ellipsis" style={{ maxWidth: 420 }} title={r.stream}>
                    {r.stream}
                  </span>
                  {r.kind === "otel_session" && <Tag tone="gray">{t("v2.tasks.logs.slice")}</Tag>}
                </span>
                {r.snippet ? (
                  <span className="sub clip" style={{ maxWidth: 520 }} title={r.snippet}>
                    {r.snippet}
                  </span>
                ) : (
                  !r.session_id && <span className="sub">{t("v2.tasks.logs.shared")}</span>
                )}
              </>
            ),
          },
          {
            key: "session",
            title: t("v2.tasks.logs.colSession"),
            render: (r: V2LogStream) => (r.session_id ? <span className="mono ellipsis" style={{ maxWidth: 200 }} title={r.session_id}>{r.session_id}</span> : "—"),
          },
          ...(q
            ? [
                {
                  key: "match",
                  title: t("v2.tasks.logs.colMatch"),
                  className: "nowrap",
                  render: (r: V2LogStream) =>
                    r.match === "content" ? (
                      <Tag tone="blue">{t("v2.tasks.logs.matchContent", { count: r.matches ?? 0 })}</Tag>
                    ) : r.match === "name" ? (
                      <Tag tone="outline">{t("v2.tasks.logs.matchName")}</Tag>
                    ) : (
                      "—"
                    ),
                },
              ]
            : []),
          ...(rows.some((r) => r.traces != null)
            ? [{ key: "traces", title: t("v2.pipelines.traceCount"), className: "num", render: (r: V2LogStream) => r.traces ?? "—" }]
            : []),
          { key: "last", title: t("v2.tasks.logs.colLast"), className: "nowrap", render: (r: V2LogStream) => fmtTime(r.last_event) },
        ]}
        rows={paged.slice}
        rowKey={(r) => `${r.stream}:${r.session_id ?? ""}`}
        loading={loading}
        error={error}
        onRetry={reload}
        empty={q ? t("v2.tasks.logs.emptyQ") : t("v2.tasks.logs.empty")}
        testId="v2-task-logs-table"
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
    </>
  );
}

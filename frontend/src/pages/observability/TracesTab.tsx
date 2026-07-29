import type { CSSProperties } from "react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { Chip, DataTable, Panel, DEFAULT_PAGE_SIZE, Pager } from "../../components";
import type { ObsTraceRow, ObsTraces } from "../../lib/api";
import { fmtClock, fmtCost, fmtDuration, fmtInt, shortId } from "./format";

const TIMEOUT_MS = 30_000;

/** Harness runtimes emit a constant stream of 2-span "InternalOperation"
 * health-check traces — noise for humans, hidden by default. */
function isSystemTrace(r: ObsTraceRow): boolean {
  return r.root_operation === "InternalOperation" && r.llm_count === 0;
}

function statusChip(row: ObsTraceRow, t: (k: string) => string) {
  if (row.status === "ok") {
    return (
      <Chip tone="good" icon="●">
        {t("obs.status.ok")}
      </Chip>
    );
  }
  return (
    <Chip tone="crit" icon="✕">
      {row.duration_ms > TIMEOUT_MS ? t("obs.status.timeout") : t("obs.status.error")}
    </Chip>
  );
}

interface TracesTabProps {
  data: ObsTraces;
  onOpenSession: (sessionId: string) => void;
  onOpenTrace: (traceId: string) => void;
}

export function TracesTab({ data, onOpenSession, onOpenTrace }: TracesTabProps) {
  const { t } = useTranslation();
  const [agent, setAgent] = useState("all");
  const [status, setStatus] = useState<"all" | "ok" | "error">("all");
  const [query, setQuery] = useState("");
  const [showSystem, setShowSystem] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  useEffect(() => {
    setPage(1); // filters change the result set — restart from page 1
  }, [agent, status, query, showSystem, data]);

  const agents = useMemo(
    () => [...new Set(data.traces.map((r) => r.agent))].sort(),
    [data.traces],
  );
  const systemCount = useMemo(
    () => data.traces.filter(isSystemTrace).length,
    [data.traces],
  );
  const rows = data.traces.filter((r) => {
    if (!showSystem && isSystemTrace(r)) return false;
    if (agent !== "all" && r.agent !== agent) return false;
    if (status !== "all" && r.status !== status) return false;
    if (query) {
      const q = query.trim().toLowerCase();
      if (
        !(r.session_id ?? "").toLowerCase().includes(q) &&
        !r.trace_id.toLowerCase().includes(q)
      ) {
        return false;
      }
    }
    return true;
  });
  const currentPage = Math.min(page, Math.max(1, Math.ceil(rows.length / pageSize)));
  const pageRows = rows.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  return (
    <Panel brk pad={false} style={{ "--i": 0 } as CSSProperties}>
      <div className="filters">
        <select
          className="fsel"
          value={agent}
          onChange={(e) => setAgent(e.target.value)}
          aria-label={t("obs.traces.agentFilter")}
        >
          <option value="all">{t("obs.traces.agentAll")}</option>
          {agents.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <button
          className={`fsel${status === "ok" ? " on-ok" : ""}`}
          onClick={() => setStatus(status === "ok" ? "all" : "ok")}
        >
          ● {t("obs.status.ok")}
        </button>
        <button
          className={`fsel${status === "error" ? " on-err" : ""}`}
          onClick={() => setStatus(status === "error" ? "all" : "error")}
        >
          ✕ {t("obs.status.error")}
        </button>
        <input
          className="fsearch"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t("obs.traces.searchPlaceholder")}
        />
        {systemCount > 0 && (
          <button
            className={`fsel${showSystem ? " on-ok" : ""}`}
            onClick={() => setShowSystem(!showSystem)}
          >
            {showSystem
              ? t("obs.traces.systemShown", { count: systemCount })
              : t("obs.traces.systemHidden", { count: systemCount })}
          </button>
        )}
        <Chip tone="muted">
          {t("obs.traces.scanned", { count: rows.length, range: data.range.toUpperCase() })}
        </Chip>
      </div>
      <DataTable
        columns={[
          { key: "time", label: t("obs.traces.cols.time") },
          { key: "op", label: t("obs.traces.cols.rootOp") },
          { key: "agent", label: t("obs.traces.cols.agent") },
          { key: "session", label: t("obs.traces.cols.session") },
          { key: "duration", label: t("obs.traces.cols.duration") },
          { key: "spans", label: t("obs.traces.cols.spans") },
          { key: "llm", label: t("obs.traces.cols.llm") },
          { key: "tokens", label: t("obs.traces.cols.tokensCost") },
          { key: "status", label: t("obs.traces.cols.status") },
          { key: "open", label: "" },
        ]}
        isEmpty={rows.length === 0}
        empty={data.traces.length === 0 ? t("obs.traces.empty") : t("obs.traces.noMatch")}
      >
        {pageRows.map((r) => (
          <tr
            key={r.trace_id}
            className="rowlink"
            title={t("obs.traces.rowHint")}
            onClick={() => onOpenTrace(r.trace_id)}
          >
            <td className="mono dim">{fmtClock(r.time)}</td>
            <td className="pri">{r.root_operation}</td>
            <td className="mono">{r.agent}</td>
            <td>
              {r.session_id ? (
                <button
                  className="sid"
                  onClick={(e) => {
                    e.stopPropagation();
                    onOpenSession(r.session_id as string);
                  }}
                  title={r.session_id}
                >
                  {shortId(r.session_id)}
                </button>
              ) : (
                <span className="mono dim">—</span>
              )}
            </td>
            <td className="mono">{fmtDuration(r.duration_ms)}</td>
            <td className="mono dim">{r.span_count}</td>
            <td className="mono dim">{r.llm_count}</td>
            <td className="mono">
              {r.tokens.total > 0
                ? `${fmtInt(r.tokens.total)} · ${fmtCost(r.est_cost_usd)}`
                : "— · —"}
            </td>
            <td>{statusChip(r, t)}</td>
            <td>
              <button
                className="rowact"
                onClick={(e) => {
                  e.stopPropagation();
                  onOpenTrace(r.trace_id);
                }}
              >
                {t("obs.traces.openWaterfall")} ▸
              </button>
            </td>
          </tr>
        ))}
      </DataTable>
      <Pager
        total={rows.length}
        page={currentPage}
        size={pageSize}
        onPage={setPage}
        onSize={(s) => {
          setPageSize(s);
          setPage(1);
        }}
      />
    </Panel>
  );
}

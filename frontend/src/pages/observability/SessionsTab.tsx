import type { CSSProperties } from "react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { Chip, DataTable, Panel, useToast, DEFAULT_PAGE_SIZE, Pager } from "../../components";
import type { ObsSessions } from "../../lib/api";
import { fmtClockShort, fmtCost, fmtInt, shortId } from "./format";

interface SessionsTabProps {
  data: ObsSessions;
  selected: string | null;
  onSelect: (sessionId: string) => void;
}

export function SessionsTab({ data, selected, onSelect }: SessionsTabProps) {
  const { t } = useTranslation();
  const toast = useToast();
  const [agent, setAgent] = useState("all");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);

  useEffect(() => {
    setPage(1); // filters change the result set — restart from page 1
  }, [agent, errorsOnly, query, data]);

  const agents = useMemo(
    () => [...new Set(data.sessions.map((r) => r.agent))].sort(),
    [data.sessions],
  );
  const rows = data.sessions.filter((r) => {
    if (agent !== "all" && r.agent !== agent) return false;
    if (errorsOnly && r.errors === 0) return false;
    if (query && !r.session_id.toLowerCase().includes(query.trim().toLowerCase())) {
      return false;
    }
    return true;
  });
  const currentPage = Math.min(page, Math.max(1, Math.ceil(rows.length / pageSize)));
  const pageRows = rows.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const copyId = (id: string) => {
    try {
      // navigator.clipboard is undefined in non-secure contexts (http LAN)
      navigator.clipboard
        .writeText(id)
        .then(() => toast(t("obs.sessions.copied"), "good"))
        .catch(() => toast(t("obs.sessions.copyFailed"), "warn"));
    } catch {
      toast(t("obs.sessions.copyFailed"), "warn");
    }
  };

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
          className={`fsel${errorsOnly ? " on-err" : ""}`}
          onClick={() => setErrorsOnly(!errorsOnly)}
        >
          ✕ {t("obs.sessions.errorsOnly")}
        </button>
        <input
          className="fsearch"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t("obs.sessions.searchPlaceholder")}
        />
        <Chip tone="muted">
          {t("obs.sessions.scanned", { count: rows.length, range: data.range.toUpperCase() })}
        </Chip>
      </div>
      <DataTable
        columns={[
          { key: "session", label: t("obs.sessions.cols.session") },
          { key: "agent", label: t("obs.sessions.cols.agent") },
          { key: "traces", label: t("obs.sessions.cols.traces") },
          { key: "llm", label: t("obs.sessions.cols.llmCalls") },
          { key: "tokens", label: t("obs.sessions.cols.tokensCost") },
          { key: "first", label: t("obs.sessions.cols.first") },
          { key: "last", label: t("obs.sessions.cols.last") },
          { key: "errors", label: t("obs.sessions.cols.errors") },
        ]}
        isEmpty={rows.length === 0}
        empty={data.sessions.length === 0 ? t("obs.sessions.empty") : t("obs.sessions.noMatch")}
      >
        {pageRows.map((r) => (
          <tr
            key={r.session_id}
            className={`rowlink${selected === r.session_id ? " sel" : ""}`}
            onClick={() => onSelect(r.session_id)}
          >
            <td>
              <span className="sid" title={r.session_id}>
                {shortId(r.session_id, 20)}
              </span>{" "}
              <button
                className="sid"
                aria-label={t("obs.sessions.copy")}
                title={t("obs.sessions.copy")}
                onClick={(e) => {
                  e.stopPropagation();
                  copyId(r.session_id);
                }}
              >
                ⧉
              </button>
            </td>
            <td className="mono">{r.agent}</td>
            <td className="mono">{r.traces}</td>
            <td className="mono dim">{r.llm_calls}</td>
            <td className="mono">
              {r.tokens.total > 0
                ? `${fmtInt(r.tokens.total)} · ${fmtCost(r.est_cost_usd)}`
                : "— · —"}
            </td>
            <td className="mono dim">{fmtClockShort(r.first)}</td>
            <td className="mono dim">{fmtClockShort(r.last)}</td>
            <td>
              {r.errors > 0 ? (
                <Chip tone="crit" icon="✕">
                  {r.errors}
                </Chip>
              ) : (
                <Chip tone="good" icon="●">
                  0
                </Chip>
              )}
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

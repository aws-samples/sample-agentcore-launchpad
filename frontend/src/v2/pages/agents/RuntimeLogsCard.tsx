import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, errorMessage, type RuntimeLogEvent, type RuntimeLogsPage, type RuntimeLogsRange } from "../../../lib/api";
import { fmtTime } from "../../format";
import { Alert, Button, Card, Segmented, Spin } from "../../ui";
import {
  appendEvents,
  logTime,
  RUNTIME_LOG_PAGE,
  RUNTIME_LOG_RANGES,
  runtimeLogsGate,
  runtimeLogsQuery,
  streamLabel,
  unavailableKey,
  validSessionId,
} from "./runtimeLogs";

interface Filters {
  range: RuntimeLogsRange;
  session: string;
  keyword: string;
  otel: boolean;
}

/** Agent detail · Runtime logs: the container's stdout/stderr, read on demand (one
 *  FilterLogEvents page per click) from the log group the backend derives. */
export function RuntimeLogsCard({
  agentId,
  hosted,
  status,
}: {
  agentId: string;
  hosted: boolean;
  /** the agent row's status: a first deploy has no Runtime yet, which is not "not hosted" */
  status: string;
}) {
  const { t } = useTranslation();
  const [filters, setFilters] = useState<Filters>({ range: "1h", session: "", keyword: "", otel: false });
  const [page, setPage] = useState<RuntimeLogsPage | null>(null);
  const [events, setEvents] = useState<RuntimeLogEvent[]>([]);
  const [sessions, setSessions] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // the filters the shown page was read with: "load more" must not mix in edits
  const applied = useRef<Filters>(filters);

  const set = <K extends keyof Filters>(key: K, value: Filters[K]) => setFilters((f) => ({ ...f, [key]: value }));
  const sessionBad = !!filters.session.trim() && !validSessionId(filters.session);

  const read = async (more: boolean) => {
    if (loading) return;
    const f = more ? applied.current : filters;
    setLoading(true);
    setError(null);
    try {
      const next = await api.agentRuntimeLogs(
        agentId,
        runtimeLogsQuery({
          range: f.range,
          session_id: f.session,
          keyword: f.keyword,
          include_otel: f.otel,
          limit: RUNTIME_LOG_PAGE,
          next_token: more ? (page?.next_token ?? undefined) : undefined,
        }),
      );
      applied.current = f;
      setPage(next);
      setEvents((prev) => (more ? appendEvents(prev, next.events) : next.events));
      if (!more && next.sessions.length) setSessions(next.sessions);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  };

  const reason = unavailableKey(page?.unavailable_reason ?? null);
  const gate = runtimeLogsGate(hosted, status);
  const listId = `v2-rtlogs-sessions-${agentId}`;

  return (
    <Card
      title={t("v2.agents.runtimeLogs.title")}
      sub={page?.log_group ? <span className="mono">{page.log_group}</span> : t("v2.agents.runtimeLogs.sub")}
      end={
        page?.cloudwatch_url ? (
          <a href={page.cloudwatch_url} target="_blank" rel="noreferrer" data-testid="v2-runtime-logs-cw">
            {t("v2.agents.runtimeLogs.openCloudWatch")}
          </a>
        ) : undefined
      }
      testId="v2-agent-runtime-logs"
    >
      {gate ? (
        <Alert>{t(`v2.agents.runtimeLogs.gate.${gate}`)}</Alert>
      ) : (
        <>
          <div className="v2-toolbar">
            <Segmented
              value={filters.range}
              options={RUNTIME_LOG_RANGES.map((r) => ({ value: r, label: t(`v2.agents.runtimeLogs.range.${r}`) }))}
              onChange={(r) => set("range", r)}
              ariaLabel={t("v2.agents.runtimeLogs.rangeLabel")}
            />
            <input
              className="v2-input mono v2-rtlogs-session"
              list={listId}
              value={filters.session}
              placeholder={t("v2.agents.runtimeLogs.sessionPlaceholder")}
              aria-label={t("v2.agents.runtimeLogs.sessionPlaceholder")}
              aria-invalid={sessionBad || undefined}
              onChange={(e) => set("session", e.target.value)}
              data-testid="v2-runtime-logs-session"
            />
            <datalist id={listId}>
              {sessions.map((sid) => (
                <option key={sid} value={sid} />
              ))}
            </datalist>
            <input
              className="v2-input v2-rtlogs-keyword"
              value={filters.keyword}
              maxLength={128}
              placeholder={t("v2.agents.runtimeLogs.keywordPlaceholder")}
              aria-label={t("v2.agents.runtimeLogs.keywordPlaceholder")}
              onChange={(e) => set("keyword", e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !sessionBad) void read(false);
              }}
            />
            <label className="v2-check" title={t("v2.agents.runtimeLogs.otelHint")}>
              <input type="checkbox" checked={filters.otel} onChange={(e) => set("otel", e.target.checked)} />
              {t("v2.agents.runtimeLogs.includeOtel")}
            </label>
            <Button kind="primary" onClick={() => void read(false)} disabled={loading || sessionBad} testId="v2-runtime-logs-query">
              {page ? t("v2.agents.runtimeLogs.query") : t("v2.agents.runtimeLogs.load")}
            </Button>
          </div>
          {sessionBad && <Alert tone="warn">{t("v2.agents.runtimeLogs.badSession")}</Alert>}
          {error && <Alert tone="error">{error}</Alert>}
          {reason && reason !== "noRuntime" && <Alert tone="warn">{t(`v2.agents.runtimeLogs.${reason}`)}</Alert>}
          {reason === "noRuntime" && <Alert>{t("v2.agents.runtimeLogs.noRuntime")}</Alert>}
          {page?.session_match === "content" && <Alert>{t("v2.agents.runtimeLogs.contentMatch")}</Alert>}
          {!!page?.hidden_unattributed && (
            <Alert>{t("v2.agents.runtimeLogs.hiddenUnattributed", { count: page.hidden_unattributed })}</Alert>
          )}
          {page?.hidden_unattributed === null && <Alert>{t("v2.agents.runtimeLogs.hiddenUnattributedFiltered")}</Alert>}
          {loading && !events.length ? (
            <Spin />
          ) : page && !reason ? (
            events.length ? (
              <pre className="v2-pre v2-rtlogs" data-testid="v2-runtime-logs-lines">
                {events.map((e, i) => (
                  <div key={`${e.timestamp}-${i}`} className="v2-rtlogs-line">
                    <span className="v2-rtlogs-ts" title={fmtTime(e.timestamp)}>
                      {logTime(e.timestamp)}
                    </span>{" "}
                    <span className="v2-rtlogs-stream" title={e.stream}>
                      [{streamLabel(e)}]
                    </span>{" "}
                    {e.message.replace(/\n$/, "")}
                  </div>
                ))}
              </pre>
            ) : (
              <span className="v2-muted">{t("v2.agents.runtimeLogs.empty")}</span>
            )
          ) : null}
          {page?.next_token && !reason && (
            <div className="v2-agents-foot">
              <Button onClick={() => void read(true)} disabled={loading}>
                {loading ? t("v2.agents.runtimeLogs.loading") : t("v2.agents.runtimeLogs.more")}
              </Button>
            </div>
          )}
        </>
      )}
    </Card>
  );
}

import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Chip, DataTable, Panel } from "../../components";
import type { MemoryExtractionJob, MemoryStrategy } from "../../lib/api";
import { api } from "../../lib/api";
import { shortId, statusTone } from "./format";
import { LoadMore } from "./LoadMore";
import { usePaged } from "./paged";

// The API's status filter accepts only FAILED (verified against the live
// service); jobs of every status still appear in the unfiltered listing.
const STATUSES = ["", "FAILED"] as const;

interface Props {
  strategies: MemoryStrategy[];
  actorId: string | null;
  sessionId: string | null;
}

/**
 * The extraction pipeline is what turns short-term events into long-term
 * records, asynchronously. Without this view an empty long-term namespace is
 * indistinguishable from a failed extraction, so the jobs get their own surface.
 *
 * Filters come from the page URL (actor/session, shared with the short-term
 * drill-down) plus local strategy/status pickers.
 */
export function ExtractionTab({ strategies, actorId, sessionId }: Props) {
  const { t } = useTranslation();
  const [strategyId, setStrategyId] = useState("");
  const [status, setStatus] = useState("");
  const [useSelection, setUseSelection] = useState(true);

  const filters = {
    actor_id: useSelection ? (actorId ?? undefined) : undefined,
    session_id: useSelection ? (sessionId ?? undefined) : undefined,
    strategy_id: strategyId || undefined,
    status: status || undefined,
  };

  const jobs = usePaged<MemoryExtractionJob>(
    (token) => api.memoryExtractionJobs(filters, token),
    [filters.actor_id, filters.session_id, strategyId, status],
  );

  return (
    <>
      <div className="filters">
        <label className="gov-demo-switch">
          <input
            type="checkbox"
            checked={useSelection}
            onChange={(e) => setUseSelection(e.target.checked)}
          />
          {t("memoryPage.extraction.useSelection")}
        </label>
        {useSelection && (
          <span className="mono dim">
            {actorId ? shortId(actorId, 10) : t("memoryPage.extraction.allActors")}
            {sessionId ? ` · ${shortId(sessionId, 8)}` : ""}
          </span>
        )}
        <select
          className="fsel"
          value={strategyId}
          onChange={(e) => setStrategyId(e.target.value)}
          aria-label={t("memoryPage.long.strategyLabel")}
        >
          <option value="">{t("memoryPage.extraction.allStrategies")}</option>
          {strategies.map((s) => (
            <option key={s.strategy_id ?? s.name} value={s.strategy_id ?? ""}>
              {s.name ?? s.strategy_id}
            </option>
          ))}
        </select>
        <select
          className="fsel"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          aria-label={t("memoryPage.extraction.statusLabel")}
        >
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s || t("memoryPage.extraction.allStatuses")}
            </option>
          ))}
        </select>
        <span className="mono dim">{t("memoryPage.extraction.statusFilterNote")}</span>
        <span className="spacer" />
        <span className="mono dim">
          {jobs.loading ? t("common.loading") : t("memoryPage.extraction.count", { count: jobs.items.length })}
        </span>
      </div>

      <Panel
        title={t("memoryPage.extraction.title")}
        sub={t("memoryPage.extraction.sub")}
        pad={false}
      >
        <DataTable
          columns={[
            { key: "job", label: t("memoryPage.extraction.colJob") },
            { key: "status", label: t("memoryPage.extraction.colStatus") },
            { key: "strategy", label: t("memoryPage.extraction.colStrategy") },
            { key: "actor", label: t("memoryPage.extraction.colActor") },
            { key: "session", label: t("memoryPage.extraction.colSession") },
            { key: "detail", label: t("memoryPage.extraction.colDetail") },
          ]}
          isEmpty={jobs.items.length === 0}
          empty={t("memoryPage.extraction.noJobs")}
        >
          {jobs.items.map((job) => (
            <tr key={job.job_id ?? `${job.actor_id}-${job.session_id}`}>
              <td className="mono" title={job.job_id ?? ""}>
                {shortId(job.job_id, 10)}
              </td>
              <td>
                <Chip tone={statusTone(job.status)}>{job.status ?? "—"}</Chip>
              </td>
              <td className="mono dim">{shortId(job.strategy_id, 8)}</td>
              <td className="mono dim" title={job.actor_id ?? ""}>
                {shortId(job.actor_id, 10)}
              </td>
              <td className="mono dim" title={job.session_id ?? ""}>
                {shortId(job.session_id, 8)}
              </td>
              <td className="mem-cell-text">
                {job.failure_reason ?? job.messages.join(" · ") ?? "—"}
              </td>
            </tr>
          ))}
        </DataTable>
        <LoadMore token={jobs.token} onClick={jobs.loadMore} />
      </Panel>
    </>
  );
}

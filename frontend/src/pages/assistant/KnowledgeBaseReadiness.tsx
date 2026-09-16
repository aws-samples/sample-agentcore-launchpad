import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api } from "../../lib/api";
import type { KnowledgeBaseDetail } from "../KnowledgeBases";

/** Read-only: selecting an existing KB never starts ingestion. */
export function KnowledgeBaseReadiness({
  kbId, name, workspaceId,
}: { kbId: string; name: string; workspaceId: string }) {
  const { t } = useTranslation();
  const [detail, setDetail] = useState<KnowledgeBaseDetail | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const read = async () => {
      try {
        const current = await api.getKnowledgeBase(kbId, workspaceId);
        if (cancelled) return;
        setDetail(current);
        setFailed(false);
        if (current.status === "CREATING" || current.data_sources.some((source) =>
          source.status === "CREATING" || source.ingestion_jobs?.some((job) =>
            ["STARTING", "IN_PROGRESS", "STOPPING"].includes(job.status)))) {
          timer = setTimeout(() => void read(), 5000);
        }
      } catch {
        if (!cancelled) setFailed(true);
      }
    };
    void read();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [kbId, workspaceId]);
  const jobs = detail?.data_sources.map((source) =>
    [...(source.ingestion_jobs ?? [])].sort((a, b) =>
      (b.started_at ?? "").localeCompare(a.started_at ?? ""))[0]);
  const indexError = detail?.status === "FAILED"
    || detail?.data_sources.some((source) => source.status === "FAILED")
    || jobs?.some((job) => job && (["FAILED", "STOPPED"].includes(job.status)
      || (job.statistics?.numberOfDocumentsFailed ?? 0) > 0));
  const completed = detail?.status === "ACTIVE" && jobs && jobs.length > 0
    && jobs.every((job) => job?.status === "COMPLETE");
  const pending = detail?.status === "CREATING" || detail?.data_sources.some(
    (source) => source.status === "CREATING",
  ) || jobs?.some((job) => job && ["STARTING", "IN_PROGRESS", "STOPPING"].includes(job.status));
  const key = failed ? "indexUnknown" : !detail ? "indexLoading"
    : indexError ? "indexFailed" : completed ? "indexComplete"
      : pending ? "indexPending" : "indexNotStarted";
  return (
    <div className="assist-kb-readiness" data-testid={`kb-readiness-${kbId}`}>
      <b>{name}</b> · {t(`assistantPreparation.${key}`)} ·{" "}
      <Link to={`/knowledge-bases?view=detail&kb=${encodeURIComponent(kbId)}`}
        target="_blank" rel="noopener noreferrer">
        {t("assistantPreparation.openKb")}
      </Link>
    </div>
  );
}

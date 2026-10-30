import { useTranslation } from "react-i18next";

/**
 * N-SCN-obs11 (r7-j): the KB detail serves each data source's `ingestion_jobs` from a
 * short cache and, when the OpenSearch lookup failed or was cut short, answers with the
 * last known list plus `jobs_degraded: true`. This is the only place that flag is
 * rendered — a small, unobtrusive hint beside the sync status; it never disables
 * anything. Renders nothing unless `degraded` is true.
 */
export function KbJobsStaleHint({ degraded, className }: { degraded?: boolean; className?: string }) {
  const { t } = useTranslation();
  if (!degraded) return null;
  return (
    <span
      className={className}
      title={t("knowledge.detail.sources.jobsStaleTitle")}
      data-testid="kb-jobs-stale"
    >
      {t("knowledge.detail.sources.jobsStale")}
    </span>
  );
}

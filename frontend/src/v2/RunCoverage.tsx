import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { coverageInfo, type RunCoverage } from "../lib/evaluation";
import { LinkButton, Tag } from "./ui";

/**
 * How a partially failed batch run is shown — shared by the architect's step 2 run
 * list and the evaluation task detail. A run whose only losses are telemetry gaps
 * (the session's traces never fully reached CloudWatch) reads as completed with its
 * coverage; anything else stays "partially failed". The raw AWS sentence moves into
 * a collapsed detail.
 */

export function RunCoverageTag({ coverage }: { coverage: RunCoverage }) {
  const { t } = useTranslation();
  return (
    <Tag tone={coverageInfo(coverage) ? "blue" : "orange"} testId="v2-run-coverage-tag">
      {coverage.kind === "telemetry_incomplete"
        ? t("v2.runCoverage.tagTelemetry", { scored: coverage.scored, total: coverage.total })
        : t("expPage.readiness.runStatus.completed_with_errors")}
    </Tag>
  );
}

export function RunCoverageNote({
  coverage,
  runId,
  rawError,
  showLink = true,
  onReadDetail,
  reading,
}: {
  coverage: RunCoverage;
  runId: string;
  rawError: string | null;
  /** hidden on the task detail page itself */
  showLink?: boolean;
  /** legacy rows: re-check the batch to backfill the reasons */
  onReadDetail?: () => void;
  reading?: boolean;
}) {
  const { t } = useTranslation();
  const counts = { failed: coverage.failed, scored: coverage.scored, total: coverage.total };
  const raw = coverage.firstError ?? rawError;
  return (
    <div className="v2-run-coverage" data-testid="v2-run-coverage" data-kind={coverage.kind}>
      <div>{t(coverage.legacy ? "v2.runCoverage.legacy" : `v2.runCoverage.${coverage.kind}`, counts)}</div>
      {coverage.excluded.length > 0 && (
        <div className="v2-muted">
          {t("v2.runCoverage.scenarios", {
            list: coverage.excluded.map((i) => `#${i}`).join(t("v2.runCoverage.scenarioSep")),
          })}
        </div>
      )}
      <div className="v2-row" style={{ gap: 12 }}>
        {showLink && !coverage.legacy && (
          <Link to={`/v2/eval/tasks?view=detail&id=${encodeURIComponent(runId)}&outcome=error`}>
            {t("v2.runCoverage.viewFailed")} ›
          </Link>
        )}
        {coverage.legacy && onReadDetail && (
          <LinkButton disabled={reading} title={t("v2.runCoverage.readDetailHint")} onClick={onReadDetail}
            testId="v2-run-coverage-read">
            {t("v2.runCoverage.readDetail")}
          </LinkButton>
        )}
      </div>
      {raw && (
        <details>
          <summary className="v2-muted">{t("v2.runCoverage.rawError")}</summary>
          <div className="mono">{raw}</div>
        </details>
      )}
    </div>
  );
}

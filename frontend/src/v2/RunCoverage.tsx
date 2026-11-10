import { useTranslation } from "react-i18next";

import { coverageInfo, type RunCoverage } from "../lib/evaluation";
import { LinkButton, Tag } from "./ui";

/**
 * How a partially failed run is shown (N12-obs47) — shared by the architect's step 2 run
 * list and the evaluation task detail. A run whose only losses are telemetry gaps (the
 * session answered but its spans never became queryable) reads as completed with its
 * coverage, in the info tone; anything else stays "partially failed" in the warning tone.
 * The first raw worker error moves into a collapsed detail.
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
  rawError,
  showRawError = true,
  onReadDetail,
  reading,
}: {
  coverage: RunCoverage;
  rawError: string | null;
  /** the collapsed first raw error; the architect's run list leaves it to the task detail */
  showRawError?: boolean;
  /** legacy rows: re-check the run to fill in which sessions were not scored */
  onReadDetail?: () => void;
  reading?: boolean;
}) {
  const { t } = useTranslation();
  const counts = {
    failed: coverage.failed,
    scored: coverage.scored,
    total: coverage.total,
    count: coverage.records?.count ?? 0,
    records: coverage.records?.total ?? 0,
  };
  const raw = coverage.firstError ?? rawError;
  return (
    <div className="v2-run-coverage" data-testid="v2-run-coverage" data-kind={coverage.kind}>
      <div>{t(coverage.legacy ? "v2.runCoverage.legacy" : `v2.runCoverage.${coverage.kind}`, counts)}</div>
      {coverage.excluded.length > 0 && (
        <div className="v2-muted" data-testid="v2-run-coverage-scenarios">
          {t("v2.runCoverage.scenarios", {
            // N12-obs62: a canary / experiment run names each unscored scenario's arm (C | T1)
            list: coverage.excluded
              .map((i) => (coverage.arms[i] ? `#${i} (${coverage.arms[i]})` : `#${i}`))
              .join(t("v2.runCoverage.scenarioSep")),
          })}
        </div>
      )}
      {coverage.legacy && onReadDetail && (
        <div>
          <LinkButton disabled={reading} title={t("v2.runCoverage.readDetailHint")} onClick={onReadDetail}
            testId="v2-run-coverage-read">
            {t("v2.runCoverage.readDetail")}
          </LinkButton>
        </div>
      )}
      {showRawError && raw && (
        <details>
          <summary className="v2-muted">{t("v2.runCoverage.rawError")}</summary>
          <div className="mono">{raw}</div>
        </details>
      )}
    </div>
  );
}

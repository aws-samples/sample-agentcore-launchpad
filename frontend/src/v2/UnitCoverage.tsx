import { useTranslation } from "react-i18next";

import { FAILURE_CLASSES, type RunTokenUsage, type UnitCoverage } from "../lib/evaluation";
import { fmtNumber } from "./format";
import { judgeTokens } from "./judgeTokens";
import { Alert } from "./ui";

/**
 * R3-f — no silent failure: how many of the units a run expected (turns, sessions, tool
 * calls) were judged, why the others were not (timeout, throttling, context overflow,
 * truncation, …) and how many produced no row at all. Shown on every run that reports it;
 * a warning below the workspace's coverage threshold (`eval_coverage_min`).
 */
export function UnitCoverageBanner({
  coverage,
  tokens,
}: {
  coverage: UnitCoverage | null | undefined;
  /** P8.3 / P8.4: the run's judge tokens and whether its token budget ran out */
  tokens?: RunTokenUsage | null;
}) {
  const { t } = useTranslation();
  if (!coverage || coverage.expected === 0) return null;
  const failures = FAILURE_CLASSES.filter((c) => (coverage.failures[c] ?? 0) > 0)
    .map((c) => `${t(`v2.unitCoverage.class.${c}`)} ${coverage.failures[c]}`);
  const pct = `${Math.round(coverage.coverage * 100)}%`;
  // N17-obs436: the judges' whole prompt as input (cache reads included), as SCORE NOW shows it
  const split = tokens ? judgeTokens(tokens.inputTokens, tokens.outputTokens, tokens.totalTokens) : null;
  return (
    <Alert tone={coverage.below_threshold ? "warn" : "info"} testId="v2-unit-coverage">
      <strong>{t("v2.unitCoverage.title", { judged: coverage.judged, expected: coverage.expected, pct })}</strong>
      {coverage.below_threshold && (
        <div data-testid="v2-unit-coverage-below">
          {t("v2.unitCoverage.below", { threshold: `${Math.round(coverage.threshold * 100)}%` })}
        </div>
      )}
      {failures.length > 0 && (
        <div className="sub">{t("v2.unitCoverage.failures", { list: failures.join(" · ") })}</div>
      )}
      {coverage.missing > 0 && <div className="sub">{t("v2.unitCoverage.missing", { n: coverage.missing })}</div>}
      {(coverage.agent_failures ?? 0) > 0 && (
        <div className="sub" data-testid="v2-unit-coverage-agent-failures">
          {t("v2.unitCoverage.agentFailures", { n: coverage.agent_failures })}
        </div>
      )}
      {tokens && (
        <div className="sub" data-testid="v2-run-tokens" title={split && split.cached > 0 ? t("v2.observability.score.tokensCached", { prompt: fmtNumber(split.prompt), cached: fmtNumber(split.cached), uncached: fmtNumber(split.uncached) }) : undefined}>
          {t("v2.unitCoverage.tokens", { input: fmtNumber(split?.prompt ?? 0), output: fmtNumber(tokens.outputTokens), total: fmtNumber(tokens.totalTokens) })}
          {tokens.exhausted && ` · ${t("v2.unitCoverage.budgetExhausted", { budget: tokens.budget ?? "—" })}`}
        </div>
      )}
    </Alert>
  );
}

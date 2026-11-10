import { useTranslation } from "react-i18next";

import type { EvaluationRunSessionResults, ExtractionCheck, ExtractionCheckSummary } from "../lib/evaluation";
import { Alert, Tag } from "./ui";

/**
 * Eval accuracy item 1: the extraction reconciliation of an evaluation — each turn's
 * extracted answer (what the judges scored) against what the agent actually answered (the
 * worker's collected replay answer, or the platform chat ledger). A mismatch means the
 * scores may not be trustworthy, so it is an error banner with the per-turn detail one
 * click away; the anomaly counters (empty answers, tool-JSON-like answers, an answer equal
 * to the previous turn's) are shown with every summary.
 */
export function ExtractionCheckBanner({
  summary,
  sessions,
}: {
  summary: ExtractionCheckSummary | null | undefined;
  /** the run's per-session results — their checks fill the mismatch detail */
  sessions?: EvaluationRunSessionResults[];
}) {
  const { t } = useTranslation();
  if (!summary) return null;
  const anomalies = summary.anomalies;
  const counters = (
    <div className="sub" data-testid="v2-extraction-anomalies">
      {t("v2.extraction.anomalies", {
        empty: anomalies.empty,
        turns: anomalies.turns,
        rate: `${Math.round((anomalies.empty_rate ?? 0) * 100)}%`,
        json: anomalies.tool_json_like,
        repeated: anomalies.repeated_previous,
      })}
    </div>
  );
  if (summary.status === "mismatch") {
    const checks = new Map((sessions ?? []).map((s) => [s.session_id, s.extraction_check]));
    return (
      <Alert tone="error" testId="v2-extraction-mismatch">
        <strong>{t("v2.extraction.mismatchTitle")}</strong>
        <div>{t("v2.extraction.mismatchBody", { mismatch: summary.mismatch, sessions: summary.sessions })}</div>
        <details data-testid="v2-extraction-detail">
          <summary>{t("v2.extraction.detail")}</summary>
          <ul>
            {summary.mismatched_sessions.map((sid) => (
              <li key={sid}>
                <span className="mono">{sid}</span>
                <MismatchList check={checks.get(sid)} />
              </li>
            ))}
          </ul>
        </details>
        {counters}
      </Alert>
    );
  }
  if (summary.status === "ok") {
    return (
      <Alert tone="success" testId="v2-extraction-ok">
        {t("v2.extraction.ok", { ok: summary.ok, turns: summary.turns_checked })}
        {summary.unverifiable > 0 && ` ${t("v2.extraction.someUnverifiable", { count: summary.unverifiable })}`}
        {counters}
      </Alert>
    );
  }
  return (
    <Alert tone="info" testId="v2-extraction-unverifiable">
      {t("v2.extraction.unverifiable", { count: summary.sessions })}
      {counters}
    </Alert>
  );
}

function MismatchList({ check }: { check: ExtractionCheck | null | undefined }) {
  const { t } = useTranslation();
  if (!check?.mismatches?.length) return null;
  return (
    <ul>
      {check.mismatches.map((m) => (
        <li key={m.turn}>
          {t("v2.extraction.turn", { turn: m.turn, expected: m.expected_head, extracted: m.extracted_head })}
        </li>
      ))}
      {check.mismatch_count > check.mismatches.length && <li>{t("v2.extraction.more", { count: check.mismatch_count - check.mismatches.length })}</li>}
    </ul>
  );
}

/** The per-row flag on a mismatching session's scores. */
export function ExtractionRowFlag() {
  const { t } = useTranslation();
  return (
    <Tag tone="red" title={t("v2.extraction.rowFlagHint")} testId="v2-extraction-row-flag">
      {t("v2.extraction.rowFlag")}
    </Tag>
  );
}

/** One session's check as a compact tag (SCORE NOW). */
export function ExtractionCheckTag({ check }: { check: ExtractionCheck | null | undefined }) {
  const { t } = useTranslation();
  if (!check) return null;
  if (check.status === "mismatch") {
    const detail = check.mismatches
      .map((m) => t("v2.extraction.turn", { turn: m.turn, expected: m.expected_head, extracted: m.extracted_head }))
      .join("\n");
    return (
      <Tag tone="red" title={detail} testId="v2-extraction-session-mismatch">
        {t("v2.extraction.mismatchTitle")}
      </Tag>
    );
  }
  if (check.status === "ok") {
    return (
      <Tag tone="green" testId="v2-extraction-session-ok">
        {t("v2.extraction.sessionOk", { turns: check.turns_checked })}
      </Tag>
    );
  }
  return (
    <Tag tone="gray" title={t("v2.extraction.unverifiableHint")} testId="v2-extraction-session-unverifiable">
      {t("v2.extraction.sessionUnverifiable")}
    </Tag>
  );
}

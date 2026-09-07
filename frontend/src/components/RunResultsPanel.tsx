import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api, errorMessage } from "../lib/api";
import type {
  EvaluationRunInfo,
  EvaluationRunResultRow,
  EvaluationRunResults,
} from "../lib/evaluation";
import { evaluatorLabel, evaluatorPolarity, scoreColor } from "../lib/evaluators";
import { Chip } from "./Chip";
import { Panel } from "./Panel";

// The results-stream read is bounded server-side (agentcore_eval.RESULT_RECORDS_MAX).
const RESULT_RECORDS_MAX = 5000;

/** Polarity-normalised mean of one session's numeric scores (a penalty
 *  evaluator scores high when the agent misbehaved, so it is inverted) —
 *  the same orientation the optimizer's worst-first selection uses. */
function sessionMean(rows: EvaluationRunResultRow[]): number | null {
  const vals = rows
    .filter((r) => r.score != null)
    .map((r) => (evaluatorPolarity(r.evaluator_id) < 0 ? 1 - (r.score as number) : r.score as number));
  return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
}

/** Per-session scores + judge explanations of the selected run — what the
 *  EVALUATOR SCORES panel's averages are made of. Same columns as SCORE NOW on
 *  the Observability session detail; each session links there for the
 *  conversation and traces. Fetched once per (run id, status): the parent
 *  replaces the run object on every poll, and a terminal batch's stream is
 *  immutable, so refetching per poll would only re-scan the log. */
export function RunResultsPanel({ run }: { run: EvaluationRunInfo | null }) {
  const { t } = useTranslation();
  const [results, setResults] = useState<EvaluationRunResults | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState<Set<string>>(new Set());
  const runId = run?.id ?? null;
  const runStatus = run?.status ?? null;

  useEffect(() => {
    setResults(null);
    setError(null);
    setOpen(new Set());
    if (!runId) return;
    let alive = true;
    setLoading(true);
    api
      .evaluationRunResults(runId)
      .then((res) => {
        if (alive) setResults(res);
      })
      .catch((err: unknown) => {
        if (alive) setError(errorMessage(err));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [runId, runStatus]);

  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const sub =
    results?.available && results.sessions.length
      ? t("evalPage.results.sub", {
          sessions: results.sessions.length,
          records: results.count,
        })
      : run
        ? `run-${run.id.slice(0, 6)} · ${run.agent_name}`
        : "—";

  let body;
  if (!run) {
    body = <div className="empty">{t("evalPage.results.empty")}</div>;
  } else if (loading) {
    body = <div className="empty">{t("evalPage.results.loading")}</div>;
  } else if (error) {
    body = (
      <div className="note" style={{ borderColor: "var(--crit)" }}>
        <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
        <span className="mono">{error}</span>
      </div>
    );
  } else if (results && !results.available) {
    body = (
      <div className="note" data-testid="run-results-unavailable">
        <span className="i">[i]</span>
        <span>
          {t(`evalPage.results.reason.${results.reason ?? "unreadable"}`)}
          {results.detail && (
            <>
              {" "}
              <span className="mono dim">{results.detail}</span>
            </>
          )}
        </span>
      </div>
    );
  } else if (results && results.sessions.length === 0) {
    body = <div className="empty">{t("evalPage.results.none")}</div>;
  } else if (results) {
    // right padding clears an overlay scrollbar, which otherwise floats over
    // the session link and the explanation ellipsis
    body = (
      <div
        style={{ maxHeight: 560, overflowY: "auto", paddingRight: 12 }}
        data-testid="run-results"
      >
        {results.truncated && (
          <div className="note" style={{ borderColor: "var(--warn)", marginBottom: 8 }}>
            <span className="i" style={{ color: "var(--warn)" }}>[!]</span>
            <span>{t("evalPage.results.truncated", { max: RESULT_RECORDS_MAX })}</span>
          </div>
        )}
        {results.sessions.map((session, si) => {
          const mean = sessionMean(session.results);
          return (
            <div key={session.session_id} style={{ marginBottom: 14 }}>
              <div
                className="mono dim"
                style={{
                  display: "flex",
                  alignItems: "baseline",
                  gap: 10,
                  fontSize: 9.5,
                  letterSpacing: ".12em",
                  margin: "6px 0",
                }}
              >
                <span>
                  {t("evalPage.results.session")} {si + 1}
                </span>
                <span title={session.session_id} style={{ letterSpacing: 0 }}>
                  {session.session_id.slice(0, 16)}…
                </span>
                {mean != null && (
                  <span style={{ letterSpacing: 0 }}>
                    {t("evalPage.results.mean")}{" "}
                    <span style={{ color: scoreColor(mean, "") }}>{mean.toFixed(2)}</span>
                  </span>
                )}
                <Link
                  to={`/observability?tab=sessions&session=${encodeURIComponent(session.session_id)}`}
                  style={{ marginLeft: "auto", letterSpacing: 0, color: "var(--amber)" }}
                >
                  {t("evalPage.results.viewSession")}
                </Link>
              </div>
              <div style={{ overflowX: "auto" }}>
                {/* Fixed layout: with auto layout, expanding one explanation to
                    pre-wrap makes the browser re-balance every column and the
                    evaluator / level / score / label columns visibly shrink.
                    Pinned widths keep them still; the explanation takes the rest. */}
                <table style={{ minWidth: 640, tableLayout: "fixed" }}>
                  <colgroup>
                    <col style={{ width: 210 }} />
                    <col style={{ width: 72 }} />
                    <col style={{ width: 72 }} />
                    <col style={{ width: 140 }} />
                    <col />
                  </colgroup>
                  <thead>
                    <tr>
                      <th>{t("evalPage.results.col.evaluator")}</th>
                      <th>{t("evalPage.results.col.level")}</th>
                      <th>{t("evalPage.results.col.score")}</th>
                      <th>{t("evalPage.results.col.label")}</th>
                      <th>{t("evalPage.results.col.explanation")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {session.results.map((r, i) => {
                      const key = `${session.session_id}:${i}`;
                      const expanded = open.has(key);
                      const failed = r.error_type != null || r.error_message != null;
                      return (
                        <tr
                          key={key}
                          onClick={() => r.explanation && toggle(key)}
                          style={{
                            cursor: r.explanation ? "pointer" : undefined,
                            verticalAlign: "top",
                          }}
                        >
                          <td title={r.evaluator_id}>
                            {evaluatorLabel(t, r.evaluator_id)}
                            <div className="mono dim" style={{ fontSize: 8.5 }}>
                              {r.evaluator_id}
                            </div>
                          </td>
                          <td className="mono dim">{r.level ?? "—"}</td>
                          <td
                            className="mono"
                            style={{
                              color:
                                r.score != null ? scoreColor(r.score, r.evaluator_id) : undefined,
                            }}
                          >
                            {r.score != null ? r.score.toFixed(2) : "—"}
                          </td>
                          <td className="mono dim">{r.label ?? "—"}</td>
                          <td
                            style={{
                              fontSize: 11,
                              whiteSpace: expanded ? "pre-wrap" : "nowrap",
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                            }}
                          >
                            {failed ? (
                              <>
                                <Chip tone="crit" icon="✕">
                                  {r.error_type ?? t("evalPage.results.failed")}
                                </Chip>
                                {r.error_message && (
                                  <div className="dim" style={{ fontSize: 10.5, marginTop: 4 }}>
                                    {r.error_message}
                                  </div>
                                )}
                              </>
                            ) : r.explanation ? (
                              `${expanded ? "▾" : "▸"} ${r.explanation}`
                            ) : (
                              "—"
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <Panel title={t("evalPage.results.title")} sub={sub} brk>
      {body}
    </Panel>
  );
}

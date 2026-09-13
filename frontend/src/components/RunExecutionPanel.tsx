import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type {
  EvaluationRunExecution,
  ExecutionCheckOutcome,
  ExecutionCheckStatus,
} from "../lib/evaluation";
import { Chip, type ChipTone } from "./Chip";
import { Panel } from "./Panel";

type Tone = ChipTone | undefined;

function statusTone(status: ExecutionCheckStatus | ExecutionCheckOutcome): Tone {
  switch (status) {
    case "pass":
      return "good";
    case "fail":
    case "error":
      return "crit";
    case "inconclusive":
    case "pending":
      return "warn";
    default:
      return undefined;
  }
}

const sessionLink = (sessionId: string) =>
  `/observability?tab=sessions&session=${encodeURIComponent(sessionId)}`;

/** Ledger of a multi-actor / multi-session procedure run: which synthetic
 *  actor + runtime session each step ran under, and the outcome of every
 *  LOCAL deterministic check. These outcomes are computed by the platform from
 *  the agent's actual replies; they are not AWS judge scores (those live in
 *  SESSION RESULTS) and they never come from the prompt or ground truth. */
export function RunExecutionPanel({ execution }: { execution: EvaluationRunExecution }) {
  const { t } = useTranslation();
  const status = execution.check_status;
  const declaredChecks = execution.scenarios.reduce((n, sc) => n + sc.checks * sc.repeat, 0);
  return (
    <Panel
      title={t("evalPage.execution.title")}
      sub={t("evalPage.execution.sub", {
        sessions: execution.sessions.length,
        steps: execution.steps.length,
        checks: execution.checks.length,
      })}
      end={
        <span style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <span className="mono dim" style={{ fontSize: 10 }}>
            {t("evalPage.execution.calls", {
              done: execution.calls_done,
              planned: execution.calls_planned,
            })}
          </span>
          <span className="mono dim" style={{ fontSize: 9.5, letterSpacing: ".12em" }}>
            {t("evalPage.execution.status")}
          </span>
          <span data-testid="execution-check-status" data-status={status}>
            <Chip tone={statusTone(status)}>{t(`evalPage.execution.statusValue.${status}`)}</Chip>
          </span>
          {execution.interrupted && (
            <span className="dim" style={{ fontSize: 10 }} data-testid="execution-interrupted">
              {t("evalPage.execution.interrupted")}
            </span>
          )}
        </span>
      }
      brk
      data-testid="run-execution"
    >
      <div className="note" style={{ marginBottom: 10 }}>
        <span className="i">[i]</span>
        <span>{t("evalPage.execution.note")}</span>
      </div>

      <div className="mono dim" style={{ fontSize: 9.5, letterSpacing: ".12em", margin: "6px 0" }}>
        {t("evalPage.execution.sessions")}
      </div>
      <div style={{ overflowX: "auto" }}>
        <table style={{ minWidth: 640 }} data-testid="execution-sessions">
          <thead>
            <tr>
              <th>{t("evalPage.execution.col.scenario")}</th>
              <th>{t("evalPage.execution.col.repeat")}</th>
              <th>{t("evalPage.execution.col.actor")}</th>
              <th>{t("evalPage.execution.col.session")}</th>
              <th>{t("evalPage.execution.col.turns")}</th>
              <th>{t("evalPage.execution.col.status")}</th>
              <th>{t("evalPage.execution.col.sessionId")}</th>
            </tr>
          </thead>
          <tbody>
            {execution.sessions.map((row) => (
              <tr key={`${row.scenario_id}:${row.repeat}:${row.actor}:${row.session}`}>
                <td className="mono">{row.scenario_id}</td>
                <td className="mono dim">r{row.repeat}</td>
                <td className="mono" title={row.actor_id}>
                  {row.actor}
                </td>
                <td className="mono">{row.session}</td>
                <td className="mono dim">{row.turns.map((n) => `T${n + 1}`).join(" ")}</td>
                <td>
                  <Chip tone={row.status === "ok" ? "good" : row.status === "partial" ? "warn" : "crit"}>
                    {t(`evalPage.execution.sessionStatus.${row.status}`)}
                  </Chip>
                  {row.drift && (
                    <span className="dim" style={{ fontSize: 10, marginLeft: 6 }}>
                      {t("evalPage.execution.drift")}
                      {row.returned_session_id && (
                        <>
                          {" · "}
                          {t("evalPage.execution.returnedSession")}{" "}
                          <Link
                            to={sessionLink(row.returned_session_id)}
                            style={{ color: "var(--amber)" }}
                            title={row.returned_session_id}
                          >
                            {row.returned_session_id.slice(0, 16)}…
                          </Link>
                        </>
                      )}
                    </span>
                  )}
                </td>
                <td className="mono" title={row.session_id}>
                  <Link to={sessionLink(row.session_id)} style={{ color: "var(--amber)" }}>
                    {row.session_id.slice(0, 16)}…
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Every invocation in order: which alias/session it ran in, the actual
          reply excerpt or the exact invoke error — the evidence the checks are
          computed from. */}
      <div className="mono dim" style={{ fontSize: 9.5, letterSpacing: ".12em", margin: "12px 0 6px" }}>
        {t("evalPage.execution.steps")}
      </div>
      {execution.steps.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ minWidth: 640, tableLayout: "fixed" }} data-testid="execution-steps">
            <colgroup>
              <col style={{ width: 140 }} />
              <col style={{ width: 60 }} />
              <col style={{ width: 60 }} />
              <col style={{ width: 60 }} />
              <col style={{ width: 90 }} />
              <col style={{ width: 90 }} />
              <col style={{ width: 130 }} />
              <col />
            </colgroup>
            <thead>
              <tr>
                <th>{t("evalPage.execution.col.scenario")}</th>
                <th>{t("evalPage.execution.col.repeat")}</th>
                <th>{t("evalPage.execution.col.step")}</th>
                <th>{t("evalPage.execution.col.turn")}</th>
                <th>{t("evalPage.execution.col.actor")}</th>
                <th>{t("evalPage.execution.col.session")}</th>
                <th>{t("evalPage.execution.col.status")}</th>
                <th>{t("evalPage.execution.col.response")}</th>
              </tr>
            </thead>
            <tbody>
              {execution.steps.map((st, i) => (
                <tr
                  key={`${st.scenario_id}:${st.repeat}:${st.index}:${i}`}
                  style={{ verticalAlign: "top" }}
                  data-testid="execution-step"
                  data-status={st.status}
                >
                  <td className="mono">{st.scenario_id}</td>
                  <td className="mono dim">r{st.repeat}</td>
                  <td className="mono dim">{st.index + 1}</td>
                  <td className="mono dim">T{st.turn + 1}</td>
                  <td className="mono">{st.actor}</td>
                  <td className="mono" title={st.session_id}>
                    <Link to={sessionLink(st.session_id)} style={{ color: "var(--amber)" }}>
                      {st.session}
                    </Link>
                  </td>
                  <td>
                    <Chip tone={st.status === "ok" ? "good" : "crit"}>
                      {t(`evalPage.execution.stepStatus.${st.status}`)}
                    </Chip>
                  </td>
                  <td style={{ fontSize: 11, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                    {st.status === "ok" ? (
                      st.response_excerpt ?? "—"
                    ) : (
                      <span style={{ color: "var(--crit)" }} data-testid="execution-step-error">
                        {st.error ?? t(`evalPage.execution.stepStatus.${st.status}`)}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {execution.steps.length === 0 && (
        <div className="empty">{t("evalPage.execution.calls", {
          done: execution.calls_done,
          planned: execution.calls_planned,
        })}</div>
      )}

      <div className="mono dim" style={{ fontSize: 9.5, letterSpacing: ".12em", margin: "12px 0 6px" }}>
        {t("evalPage.execution.checks")}
      </div>
      {execution.checks.length === 0 && declaredChecks > 0 ? (
        <div className="note" style={{ borderColor: "var(--warn)" }} data-testid="execution-checks-pending">
          <span className="i" style={{ color: "var(--warn)" }}>[!]</span>
          <span>{t("evalPage.execution.checksPending", { count: declaredChecks })}</span>
        </div>
      ) : execution.checks.length === 0 ? (
        <div className="empty" data-testid="execution-no-checks">
          {t("evalPage.execution.noChecks")}
        </div>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={{ minWidth: 640, tableLayout: "fixed" }} data-testid="execution-checks">
            <colgroup>
              <col style={{ width: 140 }} />
              <col style={{ width: 60 }} />
              <col style={{ width: 120 }} />
              <col style={{ width: 110 }} />
              <col style={{ width: 60 }} />
              <col style={{ width: 120 }} />
              <col />
            </colgroup>
            <thead>
              <tr>
                <th>{t("evalPage.execution.col.scenario")}</th>
                <th>{t("evalPage.execution.col.repeat")}</th>
                <th>{t("evalPage.execution.col.check")}</th>
                <th>{t("evalPage.execution.col.type")}</th>
                <th>{t("evalPage.execution.col.turn")}</th>
                <th>{t("evalPage.execution.col.outcome")}</th>
                <th>{t("evalPage.execution.col.evidence")}</th>
              </tr>
            </thead>
            <tbody>
              {execution.checks.map((c, i) => (
                <tr key={`${c.scenario_id}:${c.repeat}:${c.id}:${i}`} style={{ verticalAlign: "top" }}>
                  <td className="mono">{c.scenario_id}</td>
                  <td className="mono dim">r{c.repeat}</td>
                  <td className="mono" title={c.text}>
                    {c.id}
                  </td>
                  <td className="mono dim">{c.type}</td>
                  <td className="mono dim">T{c.turn + 1}</td>
                  <td>
                    <span data-testid="execution-check-outcome" data-outcome={c.outcome}>
                      <Chip tone={statusTone(c.outcome)}>
                        {t(`evalPage.execution.statusValue.${c.outcome}`)}
                      </Chip>
                    </span>
                  </td>
                  <td style={{ fontSize: 11, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                    {c.evidence}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

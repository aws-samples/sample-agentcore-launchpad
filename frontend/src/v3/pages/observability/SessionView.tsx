import { ArrowLeft, Gauge, MessagesSquare, RefreshCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";

import { Markdown } from "../../../components/Markdown";
import { api, ApiError, type ObsSessionDetail, type ObsTranscript, type OnlineSessionScores } from "../../../lib/api";
import { evaluatorLabel } from "../../../lib/evaluators";
import { fmtNumber } from "../../../v2/format";
import { approxCost, SESSION_ID_RE, shortId } from "../../../v2/pages/observability/common";
import { ago, ms } from "../../format";
import { useLoad } from "../../hooks";
import { Btn, Chip, Empty, Notice, PageHead, Panel, Skeleton, Stat } from "../../ui";
import { CopyId, Explanation, Score, TraceStatus } from "./shared";
import { onlineHref } from "../online/actions";

/** Event timestamps are UTC ISO: show the browser's clock, else the raw HH:MM:SS. */
function clock(at: string): string {
  const d = new Date(at);
  if (!Number.isNaN(d.getTime())) return d.toLocaleTimeString("en-GB", { hour12: false });
  return at.match(/\d{2}:\d{2}:\d{2}/)?.[0] ?? "";
}

function transcriptSub(t: (k: string, o?: Record<string, unknown>) => string, tr: ObsTranscript, sessionId: string): string {
  if (!tr.available) return shortId(sessionId, 20);
  const actor = tr.actor_id ?? "—";
  switch (tr.source) {
    case "eval":
      return t(tr.origin === "logs" ? "obs.session.conversationEvalLogsSub" : "obs.session.conversationEvalSub", { run: `run-${(tr.run_id ?? "").slice(0, 6)}`, actor });
    case "experiment":
      return t("obs.session.conversationExperimentSub", { exp: tr.experiment_name ?? tr.experiment_id ?? "—", actor });
    case "external":
      return t("obs.session.conversationExternalSub", { actor });
    default:
      return t("obs.session.conversationSub", { actor });
  }
}

function OnlineScores({ scores }: { scores: OnlineSessionScores }) {
  const { t } = useTranslation();
  return (
    <Panel title={t("v3.obs.onlineTitle")}
      end={<span>{scores.total > 0 ? t("v3.obs.onlineSub", { count: scores.total, configs: scores.configs.length }) : t("obs.session.onlineSubPending")}</span>}
      flush={scores.total > 0 && !scores.unavailable}>
      {scores.unavailable ? (
        <Notice s="wait">{t("obs.session.onlineUnavailable")}</Notice>
      ) : scores.total === 0 ? (
        <p style={{ margin: 0, color: "var(--v3-text-3)" }}>{t("v3.obs.onlineNone")}</p>
      ) : (
        scores.configs.map((cfg) => (
          <div key={cfg.config_id} style={{ borderTop: "1px solid var(--v3-line)" }}>
            <div style={{ display: "flex", gap: 10, alignItems: "center", padding: "12px 20px 4px" }}>
              <b title={cfg.config_id}>{cfg.config_name ?? cfg.config_id}</b>
              <Chip s={cfg.owner === "agent" ? "info" : cfg.owner === "external" ? "wait" : undefined}>{t(`evalPage.online.owner.${cfg.owner}`)}</Chip>
              {cfg.agent && <span style={{ color: "var(--v3-text-3)" }}>{t("v3.obs.onlineBy", { agent: cfg.agent.name })}</span>}
              {cfg.owner === "agent" && (
                <Link to={onlineHref({ view: "detail", id: cfg.config_id })} className="v3-btn ghost sm" style={{ marginLeft: "auto" }}>
                  {t("v3.obs.onlineOpen")}
                </Link>
              )}
            </div>
            <table className="v3-table">
              <thead>
                <tr>
                  <th>{t("v3.obs.evaluator")}</th>
                  <th className="num">{t("v3.obs.score")}</th>
                  <th>{t("v3.obs.label")}</th>
                  <th>{t("v3.obs.explanation")}</th>
                  <th className="num">{t("v3.obs.when")}</th>
                </tr>
              </thead>
              <tbody>
                {cfg.records.map((r, i) => (
                  <tr key={`${cfg.config_id}:${i}`}>
                    <td title={r.evaluator_id}>
                      {evaluatorLabel(t, r.evaluator_id)}
                      {r.level && <span className="v3-obs-sub">{t(`v2.level.${r.level}`, { defaultValue: r.level })}</span>}
                    </td>
                    <td className="num"><Score value={r.score} evaluatorId={r.evaluator_id} /></td>
                    <td>{r.label ?? "—"}</td>
                    <td style={{ maxWidth: 420 }}><Explanation text={r.explanation} /></td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(r.time)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ))
      )}
    </Panel>
  );
}

export function SessionView({
  sessionId,
  range,
  onBack,
  onOpenTrace,
}: {
  sessionId: string;
  range: string;
  onBack: () => void;
  onOpenTrace: (id: string) => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [force, setForce] = useState(0);
  const load = useLoad<ObsSessionDetail | "invalid">(
    () =>
      SESSION_ID_RE.test(sessionId)
        ? api.obsSession(sessionId, range, force > 0).catch((err: unknown) => {
            if (err instanceof ApiError && err.code === "validation.invalid_request") return "invalid" as const;
            throw err;
          })
        : Promise.resolve("invalid" as const),
    `v3-obs-session:${sessionId}:${range}:${force}`,
  );
  const detail = load.data && load.data !== "invalid" ? load.data : null;
  const notFound = load.data === "invalid" || (detail != null && detail.traces.length === 0 && !detail.transcript.available);
  const tr = detail?.transcript;
  const canChat = !!tr?.available && tr.agent_id != null && tr.source === "chat";
  const online = detail?.online_scores;
  const showOnline = online != null && (online.total > 0 || online.configs_exist);
  const agentName = tr?.agent_name ?? detail?.summary.agent ?? "Agent";
  const s = detail?.summary;
  // on-demand scoring (evaluator picker, confirm, results) stays in the full view
  const scoreHref = `/v2/observability?tab=sessions&view=session&id=${encodeURIComponent(sessionId)}${range !== "24h" ? `&range=${range}` : ""}&full=1`;

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <div>
        <button type="button" className="v3-btn ghost sm" onClick={onBack}><ArrowLeft size={14} /> {t("v3.obs.tab.sessions")}</button>
      </div>
      <PageHead
        eyebrow={`${t("v3.obs.session")} · ${shortId(sessionId, 24)}`}
        title={
          <span style={{ display: "inline-flex", gap: 12, alignItems: "center" }}>
            {s?.agent ?? t("v3.obs.session")}
            {s && s.errors > 0 && <Chip s="act">{t("v3.obs.errorCount", { count: s.errors })}</Chip>}
          </span>
        }
        sub={s ? t("v3.obs.sessionSpan", { first: ago(s.first), last: ago(s.last) }) : undefined}
        end={
          <>
            {canChat && (
              <Btn onClick={() => navigate(`/v3/chat?agent=${encodeURIComponent(tr?.agent_id ?? "")}&session=${encodeURIComponent(sessionId)}`)}>
                <MessagesSquare size={14} /> {t("v3.obs.openInChat")}
              </Btn>
            )}
            {!notFound && <Link to={scoreHref} className="v3-btn"><Gauge size={14} /> {t("v3.obs.scoreNow")}</Link>}
            <Btn kind="ghost" disabled={load.loading} onClick={() => setForce((n) => n + 1)} title={t("v3.obs.refresh")}><RefreshCw size={14} /></Btn>
          </>
        }
      />
      {load.loading && !load.data ? (
        <Panel><Skeleton rows={6} /></Panel>
      ) : load.error && !load.data ? (
        <Notice s="act">{t("obs.loadFailed", { msg: load.error })}</Notice>
      ) : notFound ? (
        <Panel><Empty title={t("v3.obs.sessionNotFound")} /></Panel>
      ) : detail && tr && s ? (
        <>
          {load.error && <Notice s="act">{t("obs.loadFailed", { msg: load.error })}</Notice>}
          <div className="v3-grid c4">
            <Panel signal={s.errors ? "act" : "ok"}><Stat label={t("v3.obs.traces")} value={s.traces} foot={<CopyId id={sessionId} chars={22} />} /></Panel>
            <Panel><Stat label={t("v3.obs.llmCalls")} value={s.llm_calls} /></Panel>
            <Panel><Stat label={t("v3.obs.tokens")} value={fmtNumber(s.tokens.total)} foot={t("v3.obs.inOut", { input: fmtNumber(s.tokens.input), output: fmtNumber(s.tokens.output) })} /></Panel>
            <Panel><Stat label={t("v3.obs.cost")} value={approxCost(s.est_cost_usd)} foot={t("obs.charts.priceNote")} /></Panel>
          </div>
          <div className="v3-obs-split">
            <Panel title={t("v3.obs.conversation")} end={<span>{transcriptSub(t, tr, sessionId)}</span>}>
              {!tr.available ? (
                <p style={{ margin: 0, color: "var(--v3-text-3)" }}>{t("v3.obs.noTranscript")}</p>
              ) : (tr.turns ?? []).length === 0 ? (
                <p style={{ margin: 0, color: "var(--v3-text-3)" }}>{t("v3.obs.noTurns")}</p>
              ) : (
                <div className="v3-obs-turns">
                  {(tr.turns ?? []).map((turn, i) => {
                    const user = turn.role === "USER";
                    return (
                      <div key={i} className={user ? "v3-obs-turn user" : "v3-obs-turn"}>
                        <span className="who">{user ? t("v3.obs.user") : agentName}<span>{clock(turn.at)}</span></span>
                        <div className="msg">{user ? turn.text : <Markdown text={turn.text} />}</div>
                      </div>
                    );
                  })}
                  {(tr.long_term_records ?? 0) > 0 && (
                    <Notice>{t("v3.obs.memNote", { count: tr.long_term_records ?? 0, actor: tr.actor_id ?? "—" })}</Notice>
                  )}
                </div>
              )}
            </Panel>
            <div className="v3-obs-side">
              <Panel title={t("v3.obs.sessionTraces")} end={<span className="mono">{detail.traces.length}</span>}>
                {detail.traces.length === 0 ? (
                  <p style={{ margin: 0, color: "var(--v3-text-3)" }}>{t("v3.obs.noSessionTraces")}</p>
                ) : (
                  <div style={{ display: "grid", gap: 8, maxHeight: 560, overflowY: "auto" }}>
                    {detail.traces.map((row) => (
                      <button key={row.trace_id} type="button" className="v3-obs-tracecard" onClick={() => onOpenTrace(row.trace_id)}>
                        <span className="h">
                          <span className="mono" style={{ color: "var(--v3-text-3)", fontSize: 12 }}>{ago(row.time)}</span>
                          <span className="op">{row.root_operation}</span>
                          <TraceStatus status={row.status} durationMs={row.duration_ms} />
                        </span>
                        <span className="m">
                          {t("v3.obs.traceCardMeta", { dur: ms(row.duration_ms), spans: row.span_count, llm: row.llm_count, tokens: fmtNumber(row.tokens.total), cost: approxCost(row.est_cost_usd) })}
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </Panel>
            </div>
          </div>
          {showOnline && online && <OnlineScores scores={online} />}
        </>
      ) : null}
    </div>
  );
}

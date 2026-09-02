import type { CSSProperties, ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";

import { Btn, Chip, Markdown, Panel } from "../../components";
import type { ChipTone } from "../../components/Chip";
import type {
  ObsSessionDetail,
  OnlineSessionScoreConfig,
  OnlineSessionScores,
} from "../../lib/api";
import { api, ApiError } from "../../lib/api";
import { evaluatorLabel, evaluatorPolarity } from "../../lib/evaluators";
import { fmtCost, fmtDuration, fmtInt, shortId } from "./format";

export const SESSION_ID_RE = /^[A-Za-z0-9_-]{8,128}$/;

/** Event timestamps are UTC ISO from the backend — render in the browser tz
 * (same as the trace cards); fall back to a raw HH:MM:SS extract. */
function turnClock(at: string): string {
  const d = new Date(at);
  if (!Number.isNaN(d.getTime()))
    return d.toLocaleTimeString("en-GB", { hour12: false });
  const match = at.match(/\d{2}:\d{2}:\d{2}/);
  return match ? match[0] : "";
}

const OWNER_TONE: Record<OnlineSessionScoreConfig["owner"], ChipTone> = {
  agent: "amber",
  experiment: "muted",
  external: "warn",
};

// Polarity-aware colour (mirrors the online page): a penalty evaluator is good
// when LOW, so its thresholds invert.
function scoreColor(score: number, evaluatorId: string): string {
  const oriented = evaluatorPolarity(evaluatorId) < 0 ? 1 - score : score;
  return oriented >= 0.7
    ? "var(--good)"
    : oriented >= 0.4
      ? "var(--warn)"
      : "var(--crit-text)";
}

/** Online evaluation results for the session, one block per config (agent-owned
 * first). Rendered only when the workspace has configs or results exist. */
function OnlineScoresPanel({ scores }: { scores: OnlineSessionScores }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState<Set<string>>(new Set());
  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  // Panel doesn't forward data-* attributes — the wrapper carries the testid and
  // spans both grid columns so the block sits full-width under conversation + traces.
  return (
    <div data-testid="obs-online-scores" style={{ gridColumn: "1 / -1" }}>
      <Panel
        title={t("obs.session.onlineTitle")}
        sub={
          scores.total > 0
            ? t("obs.session.onlineSub", {
                count: scores.total,
                configs: scores.configs.length,
              })
            : t("obs.session.onlineSubPending")
        }
        style={{ "--i": 2 } as CSSProperties}
      >
        {scores.unavailable ? (
          <div className="note">
            <span className="i">[!]</span>
            <span>{t("obs.session.onlineUnavailable")}</span>
          </div>
        ) : scores.total === 0 ? (
          <div className="empty">{t("obs.session.onlineNone")}</div>
        ) : (
          scores.configs.map((cfg, ci) => (
            <div
              key={cfg.config_id}
              data-testid={`obs-online-config-${cfg.config_id}`}
              style={{ marginTop: ci === 0 ? 0 : 14 }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  marginBottom: 6,
                }}
              >
                <span
                  className="mono"
                  style={{ fontSize: 11 }}
                  title={cfg.config_id}
                >
                  {cfg.config_name ?? cfg.config_id}
                </span>
                <Chip tone={OWNER_TONE[cfg.owner]}>
                  {t(`evalPage.online.owner.${cfg.owner}`)}
                </Chip>
                {cfg.agent && (
                  <span className="dim" style={{ fontSize: 11 }}>
                    {t("obs.session.onlineBy", { agent: cfg.agent.name })}
                  </span>
                )}
                {cfg.owner === "agent" && (
                  <Link
                    to={`/evaluation?view=online&oe=${encodeURIComponent(cfg.config_id)}`}
                    className="mono"
                    style={{
                      marginLeft: "auto",
                      fontSize: 10,
                      color: "var(--ink-3)",
                    }}
                  >
                    {t("obs.session.onlineOpenConfig")} ↗
                  </Link>
                )}
              </div>
              <div style={{ overflowX: "auto" }}>
                <table style={{ minWidth: 480 }}>
                  <thead>
                    <tr>
                      <th>{t("evalPage.online.results.col.evaluator")}</th>
                      <th>{t("evalPage.online.results.col.score")}</th>
                      <th>{t("evalPage.online.results.col.label")}</th>
                      <th>{t("evalPage.online.results.col.explanation")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cfg.records.map((r, i) => {
                      const key = `${cfg.config_id}:${i}`;
                      const expanded = open.has(key);
                      return (
                        <tr
                          key={key}
                          data-testid={`obs-online-record-${i}`}
                          onClick={() => r.explanation && toggle(key)}
                          style={{
                            cursor: r.explanation ? "pointer" : undefined,
                            verticalAlign: "top",
                          }}
                        >
                          <td title={r.evaluator_id}>
                            {evaluatorLabel(t, r.evaluator_id)}
                            {r.level && (
                              <span
                                className="mono dim"
                                style={{ fontSize: 8.5, marginLeft: 6 }}
                              >
                                {r.level}
                              </span>
                            )}
                          </td>
                          <td
                            className="mono"
                            style={{
                              color:
                                r.score != null
                                  ? scoreColor(r.score, r.evaluator_id)
                                  : undefined,
                            }}
                          >
                            {r.score != null ? r.score.toFixed(2) : "—"}
                          </td>
                          <td className="mono dim">{r.label ?? "—"}</td>
                          <td
                            style={{
                              fontSize: 11,
                              maxWidth: expanded ? undefined : 320,
                              whiteSpace: expanded ? "pre-wrap" : "nowrap",
                              overflow: "hidden",
                              textOverflow: "ellipsis",
                            }}
                          >
                            {r.explanation
                              ? `${expanded ? "▾" : "▸"} ${r.explanation}`
                              : "—"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          ))
        )}
      </Panel>
    </div>
  );
}

interface SessionDetailViewProps {
  sessionId: string;
  range: string;
  onOpenTrace: (traceId: string) => void;
}

export function SessionDetailView({
  sessionId,
  range,
  onOpenTrace,
}: SessionDetailViewProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<ObsSessionDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [invalid, setInvalid] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  // The detail renders below the (long) sessions table — bring it into view
  // when a session is picked, otherwise the click looks like a no-op. Runs
  // again once data lands: the layout above shifts while loading, which
  // strands the first (smooth) scroll short of the target.
  const loaded = detail != null;
  useEffect(() => {
    wrapRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [sessionId, loaded]);

  useEffect(() => {
    setDetail(null);
    setError(null);
    if (!SESSION_ID_RE.test(sessionId)) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    // The component stays mounted across sessionId/range changes, so a slow
    // (cache-miss) response must not overwrite a faster later one.
    let alive = true;
    api
      .obsSession(sessionId, range)
      .then((res) => {
        if (alive) setDetail(res);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        if (
          err instanceof ApiError &&
          err.code === "validation.invalid_request"
        ) {
          setInvalid(true);
        } else {
          setError(err instanceof Error ? err.message : String(err));
        }
      });
    return () => {
      alive = false;
    };
  }, [sessionId, range]);

  // Single scrollable anchor around every render state (66px clears the
  // sticky topbar).
  const wrap = (children: ReactNode) => (
    <div ref={wrapRef} style={{ scrollMarginTop: 66 }}>
      {children}
    </div>
  );

  if (invalid) {
    return wrap(
      <Panel brk style={{ marginTop: 14 } as CSSProperties}>
        <div className="empty">{t("obs.session.notFound")}</div>
      </Panel>,
    );
  }
  if (error != null) {
    return wrap(
      <Panel brk style={{ marginTop: 14 } as CSSProperties}>
        <div className="obs-error">
          <span>{t("obs.loadFailed", { msg: error })}</span>
        </div>
      </Panel>,
    );
  }
  if (detail == null) {
    return wrap(
      <Panel brk style={{ marginTop: 14 } as CSSProperties}>
        <div className="loading-line">{t("common.loading")}</div>
      </Panel>,
    );
  }

  const { transcript, traces, summary } = detail;
  // additive field — an older backend omits it; hide the panel entirely for
  // workspaces that never created an online config and have no results
  const onlineScores = detail.online_scores;
  const showOnline =
    onlineScores != null &&
    (onlineScores.total > 0 || onlineScores.configs_exist);
  if (traces.length === 0 && !transcript.available) {
    return wrap(
      <Panel brk style={{ marginTop: 14 } as CSSProperties}>
        <div className="empty">{t("obs.session.notFound")}</div>
      </Panel>,
    );
  }
  const agentLabel = (
    transcript.agent_name ??
    summary.agent ??
    "agent"
  ).toUpperCase();
  // Where the conversation was read from — eval runs, experiment gateway traffic
  // and external /v1 callers all live outside the chat ledger, so name the
  // origin (and the memory actor it was read under) instead of pretending it is
  // a console conversation.
  const conversationSub = (): string => {
    if (!transcript.available) return shortId(sessionId, 20);
    const actor = transcript.actor_id ?? "—";
    switch (transcript.source) {
      case "eval":
        return t(
          transcript.origin === "logs"
            ? "obs.session.conversationEvalLogsSub"
            : "obs.session.conversationEvalSub",
          { run: `run-${(transcript.run_id ?? "").slice(0, 6)}`, actor },
        );
      case "experiment":
        return t("obs.session.conversationExperimentSub", {
          exp: transcript.experiment_name ?? transcript.experiment_id ?? "—",
          actor,
        });
      case "external":
        return t("obs.session.conversationExternalSub", { actor });
      default:
        return t("obs.session.conversationSub", { actor });
    }
  };

  return wrap(
    <div className="grid-31" style={{ marginTop: 14 }}>
      <Panel
        brk
        title={t("obs.session.conversation")}
        sub={conversationSub()}
        end={
          // only chat-ledger sessions can be resumed; eval/experiment/external
          // sessions have no ledger row behind them
          transcript.available &&
          transcript.agent_id != null &&
          transcript.source === "chat" ? (
            <Btn
              onClick={() =>
                navigate(
                  `/chat?agent=${encodeURIComponent(transcript.agent_id ?? "")}&session=${encodeURIComponent(sessionId)}`,
                )
              }
            >
              {t("obs.session.openInChat")} ↗
            </Btn>
          ) : undefined
        }
        style={{ "--i": 0 } as CSSProperties}
      >
        {!transcript.available ? (
          <div className="empty">{t("obs.session.noTranscript")}</div>
        ) : (transcript.turns ?? []).length === 0 ? (
          <div className="empty">{t("obs.session.noTurns")}</div>
        ) : (
          <>
            {(transcript.turns ?? []).map((turn, i) => {
              const isUser = turn.role === "USER";
              return (
                <div className={`turn ${isUser ? "user" : "agent"}`} key={i}>
                  <div className="who">
                    {isUser ? t("obs.session.user") : agentLabel} ·{" "}
                    {turnClock(turn.at)}
                  </div>
                  <div className="msg">
                    <Markdown text={turn.text} />
                  </div>
                </div>
              );
            })}
            {(transcript.long_term_records ?? 0) > 0 && (
              <div className="memnote">
                ◈{" "}
                {t("obs.session.memnote", {
                  count: transcript.long_term_records ?? 0,
                  actor: transcript.actor_id ?? "—",
                })}
              </div>
            )}
          </>
        )}
      </Panel>
      <Panel
        title={t("obs.session.tracesTitle")}
        sub={t("obs.session.tracesSub", { count: traces.length })}
        style={{ "--i": 1 } as CSSProperties}
      >
        {traces.length === 0 ? (
          <div className="empty">{t("obs.session.noTraces")}</div>
        ) : (
          traces.map((tr) => (
            <button
              className="tracecard"
              key={tr.trace_id}
              onClick={() => onOpenTrace(tr.trace_id)}
            >
              <div className="tc-h">
                <span className="cat llm" />
                {tr.time != null
                  ? new Date(tr.time).toLocaleTimeString("en-GB", {
                      hour12: false,
                    })
                  : "—"}{" "}
                · {tr.root_operation}
                {tr.status === "ok" ? (
                  <Chip tone="good" icon="●" style={{ marginLeft: "auto" }}>
                    {t("obs.status.ok")}
                  </Chip>
                ) : (
                  <Chip tone="crit" icon="✕" style={{ marginLeft: "auto" }}>
                    {t("obs.status.error")}
                  </Chip>
                )}
              </div>
              <div className="tc-m">
                {fmtDuration(tr.duration_ms)} · {tr.span_count} spans ·{" "}
                {tr.llm_count} llm · {fmtInt(tr.tokens.total)} tok · ≈
                {fmtCost(tr.est_cost_usd)}
              </div>
            </button>
          ))
        )}
      </Panel>
      {showOnline && <OnlineScoresPanel scores={onlineScores} />}
    </div>,
  );
}

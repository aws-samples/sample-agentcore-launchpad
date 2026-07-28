import type { CSSProperties, ReactNode } from "react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { Btn, Chip, Markdown, Panel } from "../../components";
import type { ObsSessionDetail } from "../../lib/api";
import { api, ApiError } from "../../lib/api";
import { fmtCost, fmtDuration, fmtInt, shortId } from "./format";

export const SESSION_ID_RE = /^[A-Za-z0-9_-]{8,128}$/;

/** Event timestamps are UTC ISO from the backend — render in the browser tz
 * (same as the trace cards); fall back to a raw HH:MM:SS extract. */
function turnClock(at: string): string {
  const d = new Date(at);
  if (!Number.isNaN(d.getTime())) return d.toLocaleTimeString("en-GB", { hour12: false });
  const match = at.match(/\d{2}:\d{2}:\d{2}/);
  return match ? match[0] : "";
}

interface SessionDetailViewProps {
  sessionId: string;
  range: string;
  onOpenTrace: (traceId: string) => void;
}

export function SessionDetailView({ sessionId, range, onOpenTrace }: SessionDetailViewProps) {
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
        if (err instanceof ApiError && err.code === "validation.invalid_request") {
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
  if (traces.length === 0 && !transcript.available) {
    return wrap(
      <Panel brk style={{ marginTop: 14 } as CSSProperties}>
        <div className="empty">{t("obs.session.notFound")}</div>
      </Panel>,
    );
  }
  const agentLabel = (transcript.agent_name ?? summary.agent ?? "agent").toUpperCase();
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
          transcript.available && transcript.agent_id != null && transcript.source === "chat" ? (
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
                    {isUser ? t("obs.session.user") : agentLabel} · {turnClock(turn.at)}
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
            <button className="tracecard" key={tr.trace_id} onClick={() => onOpenTrace(tr.trace_id)}>
              <div className="tc-h">
                <span className="cat llm" />
                {tr.time != null ? new Date(tr.time).toLocaleTimeString("en-GB", { hour12: false }) : "—"}{" "}
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
                {fmtDuration(tr.duration_ms)} · {tr.span_count} spans · {tr.llm_count} llm ·{" "}
                {fmtInt(tr.tokens.total)} tok · ≈{fmtCost(tr.est_cost_usd)}
              </div>
            </button>
          ))
        )}
      </Panel>
    </div>
  );
}

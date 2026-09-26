import { MessageSquare, Play, RefreshCw } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { Markdown } from "../../../components/Markdown";
import {
  api,
  ApiError,
  type EvaluatorRow,
  type ObsSessionDetail,
  type ObsSessionScore,
  type ObsSessionScoreResult,
  type ObsTranscript,
  type OnlineSessionScoreConfig,
  type OnlineSessionScoreRecord,
  type OnlineSessionScores,
  type V2Range,
} from "../../../lib/api";
import { evaluatorLabel } from "../../../lib/evaluators";
import { EvaluatorPicker } from "../../EvaluatorPicker";
import { fmtDuration, fmtNumber, fmtScore, fmtTime, normalizedScore, scoreTone } from "../../format";
import { useLoad } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  Descriptions,
  FlowHeader,
  LinkButton,
  Spin,
  Table,
  Tag,
  type TagTone,
} from "../../ui";
import { approxCost, MAX_SCORE_EVALUATORS, SESSION_ID_RE, shortId } from "./common";
import { CopyId } from "./SessionList";
import { TraceStatusTag } from "./TraceList";

const OWNER_TONE: Record<OnlineSessionScoreConfig["owner"], TagTone> = {
  agent: "blue",
  experiment: "gray",
  external: "orange",
};

/** Event timestamps are UTC ISO — render in the browser tz; fall back to a raw HH:MM:SS extract. */
function turnClock(at: string): string {
  const d = new Date(at);
  if (!Number.isNaN(d.getTime())) return d.toLocaleTimeString("en-GB", { hour12: false });
  const match = at.match(/\d{2}:\d{2}:\d{2}/);
  return match ? match[0] : "";
}

function Score({ value, evaluatorId }: { value: number | null; evaluatorId: string }) {
  if (value == null) return <span className="v2-muted">—</span>;
  return <span className={`v2-score ${scoreTone(normalizedScore(value, evaluatorId))}`}>{fmtScore(value)}</span>;
}

/** Click-to-expand explanation cell (clamped to two lines until opened). */
function Explanation({ text }: { text: string | null }) {
  const [open, setOpen] = useState(false);
  if (!text) return <span className="v2-muted">—</span>;
  return (
    <button type="button" className={`v2-observability-expl${open ? " open" : ""}`} onClick={() => setOpen((v) => !v)}>
      {text}
    </button>
  );
}

function OnlineScores({ scores }: { scores: OnlineSessionScores }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const columns: Column<OnlineSessionScoreRecord & { i: number }>[] = [
    {
      key: "evaluator",
      title: t("evalPage.online.results.col.evaluator"),
      render: (r) => (
        <span title={r.evaluator_id}>
          {evaluatorLabel(t, r.evaluator_id)}
          {r.level && <span className="sub">{t(`v2.level.${r.level}`, { defaultValue: r.level })}</span>}
        </span>
      ),
    },
    { key: "score", title: t("evalPage.online.results.col.score"), className: "num", render: (r) => <Score value={r.score} evaluatorId={r.evaluator_id} /> },
    { key: "label", title: t("evalPage.online.results.col.label"), render: (r) => r.label ?? "—" },
    { key: "expl", title: t("evalPage.online.results.col.explanation"), render: (r) => <Explanation text={r.explanation} /> },
    { key: "time", title: t("v2.observability.col.time"), className: "nowrap", render: (r) => fmtTime(r.time) },
  ];
  return (
    <Card
      title={t("v2.observability.online.title")}
      sub={
        scores.total > 0
          ? t("v2.observability.online.sub", { count: scores.total, configs: scores.configs.length })
          : t("obs.session.onlineSubPending")
      }
      testId="v2-obs-online-scores"
    >
      {scores.unavailable ? (
        <Alert tone="warn">{t("obs.session.onlineUnavailable")}</Alert>
      ) : scores.total === 0 ? (
        <div className="v2-observability-empty">{t("v2.observability.online.none")}</div>
      ) : (
        scores.configs.map((cfg) => (
          <div key={cfg.config_id} className="v2-observability-config" data-testid={`v2-obs-online-config-${cfg.config_id}`}>
            <div className="v2-row">
              <b title={cfg.config_id}>{cfg.config_name ?? cfg.config_id}</b>
              <Tag tone={OWNER_TONE[cfg.owner]}>{t(`evalPage.online.owner.${cfg.owner}`)}</Tag>
              {cfg.agent && <span className="v2-muted">{t("v2.observability.online.by", { agent: cfg.agent.name })}</span>}
              {cfg.owner === "agent" && (
                <span style={{ marginLeft: "auto" }}>
                  <LinkButton
                    onClick={() =>
                      navigate(`/v2/eval/online?view=detail&id=${encodeURIComponent(cfg.config_id)}`)
                    }
                  >
                    {t("v2.observability.online.openConfig")}
                  </LinkButton>
                </span>
              )}
            </div>
            <Table
              density="dense"
              columns={columns}
              rows={cfg.records.map((r, i) => ({ ...r, i }))}
              rowKey={(r) => `${cfg.config_id}:${r.i}`}
            />
          </div>
        ))
      )}
    </Card>
  );
}

/** 立即评分 — synchronous data-plane Evaluate over this session's spans; nothing is persisted. */
function ScoreNow({ sessionId, range }: { sessionId: string; range: V2Range }) {
  const { t } = useTranslation();
  const evaluators = useLoad(() => api.v2Evaluators(), "obs-score-evaluators");
  const [chosen, setChosen] = useState<string[]>([]);
  const [confirming, setConfirming] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ObsSessionScore | null>(null);
  const [error, setError] = useState<{ code: string | null; message: string } | null>(null);
  const list = useMemo(() => evaluators.data?.evaluators ?? [], [evaluators.data]);
  // the picker is long — bring the outcome into view once it lands
  const outcomeRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (result || error) outcomeRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [result, error]);

  const nameOf = (id: string): string => {
    const row = list.find((e) => e.id === id);
    return row?.source === "custom" ? (row.name ?? id) : evaluatorLabel(t, id);
  };

  const run = async () => {
    setConfirming(false);
    if (chosen.length === 0 || running) return;
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      setResult(await api.obsEvaluateSession(sessionId, { evaluator_ids: chosen, range }));
    } catch (err: unknown) {
      setError(
        err instanceof ApiError
          ? { code: err.code, message: t(`apiErrors.${err.code}`, err.message) }
          : { code: null, message: err instanceof Error ? err.message : String(err) },
      );
    } finally {
      setRunning(false);
    }
  };

  const spansMissing = error?.code === "observability.session_spans_missing";
  const columns: Column<ObsSessionScoreResult & { i: number }>[] = [
    {
      key: "evaluator",
      title: t("evalPage.online.results.col.evaluator"),
      render: (r) => (
        <span title={r.evaluator_arn ?? r.evaluator_id}>
          {r.evaluator_name ?? nameOf(r.evaluator_id)}
          <span className="sub mono">{r.evaluator_id}</span>
        </span>
      ),
    },
    { key: "value", title: t("evalPage.online.results.col.score"), className: "num", render: (r) => <Score value={r.value} evaluatorId={r.evaluator_id} /> },
    { key: "label", title: t("evalPage.online.results.col.label"), render: (r) => r.label ?? "—" },
    { key: "expl", title: t("evalPage.online.results.col.explanation"), render: (r) => <Explanation text={r.explanation} /> },
    {
      key: "tokens",
      title: t("v2.observability.score.tokens"),
      className: "num nowrap",
      render: (r) => (r.token_usage ? `${fmtNumber(r.token_usage.input ?? 0)} / ${fmtNumber(r.token_usage.output ?? 0)}` : "—"),
    },
    {
      key: "status",
      title: t("v2.observability.col.status"),
      render: (r) =>
        r.error_code != null ? (
          <>
            <Tag tone="red">{r.error_code}</Tag>
            {r.error_message && <span className="sub">{r.error_message}</span>}
          </>
        ) : (
          <Tag tone="green">{t("v2.observability.score.ok")}</Tag>
        ),
    },
  ];

  return (
    <Card
      title={t("v2.observability.score.title")}
      sub={t("v2.observability.score.sub", { count: chosen.length, max: MAX_SCORE_EVALUATORS })}
      end={
        <Button
          kind="primary"
          size="sm"
          disabled={chosen.length === 0 || running}
          title={chosen.length === 0 ? t("v2.observability.score.pick") : undefined}
          onClick={() => setConfirming(true)}
          testId="v2-obs-score-run"
        >
          <Play size={12} aria-hidden="true" />
          {running ? t("v2.observability.score.running") : t("v2.observability.score.run")}
        </Button>
      }
      testId="v2-obs-score-now"
    >
      <Alert tone="info">{t("obs.session.score.hint")}</Alert>
      <EvaluatorPicker
        evaluators={list}
        loading={evaluators.loading}
        error={evaluators.error}
        onRetry={evaluators.reload}
        selected={chosen}
        onChange={(next) => setChosen(next.slice(0, MAX_SCORE_EVALUATORS))}
        max={MAX_SCORE_EVALUATORS}
        // on-demand scoring sends session spans only (no ground truth)
        blockedReason={(e: EvaluatorRow) => (e.requires_ground_truth ? t("obs.session.score.needsGroundTruth") : null)}
        testIdPrefix="v2-obs-score"
      />
      {running && <Spin label={t("v2.observability.score.running")} />}
      {error && (
        <div style={{ marginTop: 12 }} data-testid="v2-obs-score-error">
          <Alert tone={spansMissing ? "warn" : "error"}>
            {error.message}
            {spansMissing && ` ${t("obs.session.score.spansMissingHint")}`}
          </Alert>
        </div>
      )}
      {result && (
        <>
          <div className="v2-sub-title">
            {t("v2.observability.score.resultSub", { spans: result.span_count, results: result.results.length })}
          </div>
          <Table
            columns={columns}
            rows={result.results.map((r, i) => ({ ...r, i }))}
            rowKey={(r) => `${r.evaluator_id}:${r.i}`}
            testId="v2-obs-score-results"
          />
        </>
      )}
      <div ref={outcomeRef} />
      <Confirm
        open={confirming}
        title={t("v2.observability.score.confirmTitle")}
        body={t("v2.observability.score.confirmBody", { count: chosen.length })}
        confirmLabel={t("v2.observability.score.run")}
        onConfirm={() => void run()}
        onClose={() => setConfirming(false)}
      />
    </Card>
  );
}

function conversationSub(t: (k: string, o?: Record<string, unknown>) => string, tr: ObsTranscript, sessionId: string): string {
  if (!tr.available) return shortId(sessionId, 20);
  const actor = tr.actor_id ?? "—";
  switch (tr.source) {
    case "eval":
      return t(tr.origin === "logs" ? "obs.session.conversationEvalLogsSub" : "obs.session.conversationEvalSub", {
        run: `run-${(tr.run_id ?? "").slice(0, 6)}`,
        actor,
      });
    case "experiment":
      return t("obs.session.conversationExperimentSub", { exp: tr.experiment_name ?? tr.experiment_id ?? "—", actor });
    case "external":
      return t("obs.session.conversationExternalSub", { actor });
    default:
      return t("obs.session.conversationSub", { actor });
  }
}

export function SessionDetail({ sessionId, range }: { sessionId: string; range: V2Range }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [, setParams] = useSearchParams();
  const [force, setForce] = useState(0);
  const load = useLoad<ObsSessionDetail | "invalid">(
    () =>
      SESSION_ID_RE.test(sessionId)
        ? api.obsSession(sessionId, range, force > 0).catch((err: unknown) => {
            if (err instanceof ApiError && err.code === "validation.invalid_request") return "invalid" as const;
            throw err;
          })
        : Promise.resolve("invalid" as const),
    `obs-session:${sessionId}:${range}:${force}`,
  );

  const rangeParam: Record<string, string> = range !== "24h" ? { range } : {};
  const back = () => setParams({ tab: "sessions", ...rangeParam });
  const openTrace = (id: string) => setParams({ tab: "traces", view: "trace", id, ...rangeParam });

  const detail = load.data && load.data !== "invalid" ? load.data : null;
  const notFound =
    load.data === "invalid" || (detail != null && detail.traces.length === 0 && !detail.transcript.available);
  const transcript = detail?.transcript;
  const canChat = !!transcript?.available && transcript.agent_id != null && transcript.source === "chat";
  const onlineScores = detail?.online_scores;
  const showOnline = onlineScores != null && (onlineScores.total > 0 || onlineScores.configs_exist);
  const agentName = transcript?.agent_name ?? detail?.summary.agent ?? "Agent";

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            <span title={sessionId}>{t("v2.observability.sessionTitle", { id: shortId(sessionId, 20) })}</span>
            {detail && !notFound && detail.summary.agent && <Tag tone="blue">{detail.summary.agent}</Tag>}
            {detail && !notFound && detail.summary.errors > 0 && (
              <Tag tone="red">{t("v2.observability.errorCount", { count: detail.summary.errors })}</Tag>
            )}
          </span>
        }
        onBack={back}
        end={
          <>
            {canChat && (
              <Button
                onClick={() =>
                  navigate(
                    `/v2/chat?agent=${encodeURIComponent(transcript?.agent_id ?? "")}&session=${encodeURIComponent(sessionId)}`,
                  )
                }
                testId="v2-obs-open-chat"
              >
                <MessageSquare size={14} aria-hidden="true" />
                {t("v2.observability.openInChat")}
              </Button>
            )}
            <Button onClick={() => setForce((n) => n + 1)} disabled={load.loading}>
              <RefreshCw size={14} aria-hidden="true" />
              {t("v2.common.refresh")}
            </Button>
          </>
        }
      />
      {load.loading && !load.data ? (
        <Spin />
      ) : load.error && !load.data ? (
        <Alert tone="error" action={<LinkButton onClick={load.reload}>{t("v2.common.retry")}</LinkButton>}>
          {t("obs.loadFailed", { msg: load.error })}
        </Alert>
      ) : notFound ? (
        <Card>
          <div className="v2-observability-empty" data-testid="v2-obs-session-notfound">
            {t("v2.observability.sessionNotFound")}
          </div>
        </Card>
      ) : detail && transcript ? (
        <>
          {load.error && <Alert tone="error">{t("obs.loadFailed", { msg: load.error })}</Alert>}
          <Card title={t("v2.observability.summary")}>
            <Descriptions
              items={[
                {
                  label: "Session ID",
                  value: (
                    <span className="v2-observability-idcell">
                      <span className="mono">{sessionId}</span>
                      <CopyId id={sessionId} />
                    </span>
                  ),
                },
                { label: "Agent", value: detail.summary.agent ?? "—" },
                { label: t("v2.observability.col.traces"), value: detail.summary.traces },
                { label: t("v2.observability.col.llmCalls"), value: detail.summary.llm_calls },
                {
                  label: "Tokens",
                  value: t("v2.observability.tokensDetail", {
                    total: fmtNumber(detail.summary.tokens.total),
                    input: fmtNumber(detail.summary.tokens.input),
                    output: fmtNumber(detail.summary.tokens.output),
                  }),
                },
                { label: t("v2.observability.span.cost"), value: approxCost(detail.summary.est_cost_usd) },
                { label: t("v2.observability.firstSeen"), value: fmtTime(detail.summary.first) },
                { label: t("v2.observability.lastSeen"), value: fmtTime(detail.summary.last) },
              ]}
            />
          </Card>
          <div className="v2-observability-trace">
            <Card title={t("v2.observability.conversation")} sub={conversationSub(t, transcript, sessionId)} testId="v2-obs-conversation">
              {!transcript.available ? (
                <div className="v2-observability-empty">{t("v2.observability.noTranscript")}</div>
              ) : (transcript.turns ?? []).length === 0 ? (
                <div className="v2-observability-empty">{t("v2.observability.noTurns")}</div>
              ) : (
                <>
                  {(transcript.turns ?? []).map((turn, i) => {
                    const isUser = turn.role === "USER";
                    return (
                      <div className={isUser ? "v2-turn user" : "v2-turn"} key={i}>
                        <span className="who">
                          {isUser ? t("v2.observability.user") : agentName}
                          <span className="v2-observability-clock">{turnClock(turn.at)}</span>
                        </span>
                        <div className="msg v2-observability-md">
                          <Markdown text={turn.text} />
                        </div>
                      </div>
                    );
                  })}
                  {(transcript.long_term_records ?? 0) > 0 && (
                    <Alert tone="info">
                      {t("v2.observability.memnote", {
                        count: transcript.long_term_records ?? 0,
                        actor: transcript.actor_id ?? "—",
                      })}
                    </Alert>
                  )}
                </>
              )}
            </Card>
            <div className="v2-observability-side">
              <Card
                title={t("v2.observability.sessionTraces")}
                sub={t("v2.observability.sessionTracesSub", { count: detail.traces.length })}
                testId="v2-obs-session-traces"
              >
                {detail.traces.length === 0 ? (
                  <div className="v2-observability-empty">{t("v2.observability.noSessionTraces")}</div>
                ) : (
                  <div className="v2-stack">
                    {detail.traces.map((tr) => (
                      <button type="button" className="v2-observability-tracecard" key={tr.trace_id} onClick={() => openTrace(tr.trace_id)}>
                        <span className="h">
                          <span className="mono">{fmtTime(tr.time)}</span>
                          <span className="op">{tr.root_operation}</span>
                          <TraceStatusTag status={tr.status} durationMs={tr.duration_ms} />
                        </span>
                        <span className="m">
                          {t("v2.observability.traceCardMeta", {
                            dur: fmtDuration(tr.duration_ms),
                            spans: tr.span_count,
                            llm: tr.llm_count,
                            tokens: fmtNumber(tr.tokens.total),
                            cost: approxCost(tr.est_cost_usd),
                          })}
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </Card>
            </div>
          </div>
          {showOnline && onlineScores && <OnlineScores scores={onlineScores} />}
          <ScoreNow sessionId={sessionId} range={range} />
        </>
      ) : null}
    </>
  );
}

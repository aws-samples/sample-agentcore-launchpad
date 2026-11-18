import { DatabaseZap, Download, Plus, ThumbsDown, ThumbsUp } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { api, feedbackApi, type V2Range } from "../../../lib/api";
import { evaluatorLabel, evaluatorScoreLabel, FAILURE_ANALYSIS_EVALUATOR } from "../../../lib/evaluators";
import { hasInsightTrees } from "../../../lib/evaluation";
import { downloadCsv, fmtScore, fmtTime } from "../../../v2/format";
import { AddToDatasetModal } from "../../../v2/pages/data/AddToDataset";
import type { ResultRow, ResultSummary } from "../../../v2/results";
import type { V2Task } from "../../../v2/tasks";
import { V2ToastProvider } from "../../../v2/ui";
import { ago } from "../../format";
import { useLoad } from "../../hooks";
import { Btn, Chip, Dialog, Empty, Filters, Lamp, Notice, Panel, type Signal, Skeleton, Stat } from "../../ui";
import { mergeClusters, type MergedCluster, type Section, SECTIONS } from "./data";
import { scoreSignal } from "./signal";

const OUTCOME_SIGNAL: Record<ResultRow["outcome"], Signal> = {
  passed: "ok",
  failed: "act",
  error: "wait",
};

export function Score({ value }: { value: number | null }) {
  if (value == null) return <span style={{ color: "var(--v3-text-3)" }}>—</span>;
  return (
    <span className="v3-ins-score" data-s={scoreSignal(value)}>
      {fmtScore(value)}
    </span>
  );
}

/**
 * V2's "add to dataset" flow (POST /api/eval/datasets/from-sessions), reused as is:
 * hosted in a `.v2.v3-host` wrapper so it picks up the V3 theme.
 */
export function AddToDataset({ sessionIds, range, onClose }: { sessionIds: string[]; range: V2Range; onClose: () => void }) {
  return (
    <V2ToastProvider>
      <div className="v2 v3-host">
        <AddToDatasetModal open sessionIds={sessionIds} range={range} onClose={onClose} />
      </div>
    </V2ToastProvider>
  );
}

/* ── KPIs + per-evaluator means ─────────────────────────────────────────── */

export function SummaryStats({ summary }: { summary: ResultSummary }) {
  const { t } = useTranslation();
  const judged = summary.total - summary.errors;
  const passRate = judged > 0 ? summary.passed / judged : null;
  const meanSignal = summary.mean == null ? undefined : summary.mean >= 0.7 ? "ok" : "act";
  return (
    <div className="v3-grid c4">
      <Panel>
        <Stat
          label={t("v2.insights.kpiCount")}
          value={summary.total}
          foot={t("v2.insights.kpiCountSub", {
            passed: summary.passed,
            failed: summary.failed,
            errors: summary.errors,
          })}
        />
      </Panel>
      <Panel signal={meanSignal}>
        <Stat
          label={t("v2.insights.kpiMean")}
          value={summary.mean == null ? "—" : summary.mean.toFixed(2)}
          signal={meanSignal}
          foot={t("v2.insights.kpiMeanSub")}
        />
      </Panel>
      <Panel signal={summary.failed > 0 ? "act" : undefined}>
        <Stat
          label="Bad Case"
          value={summary.failed}
          signal={summary.failed > 0 ? "act" : undefined}
          foot={t("v2.insights.kpiBadSub")}
        />
      </Panel>
      <Panel>
        <Stat
          label={t("v2.insights.kpiPassRate")}
          value={passRate == null ? "—" : `${Math.round(passRate * 100)}%`}
          foot={t("v2.insights.kpiPassRateSub")}
        />
      </Panel>
    </div>
  );
}

export function EvaluatorBars({ summary }: { summary: ResultSummary }) {
  const { t } = useTranslation();
  if (summary.byEvaluator.length === 0) return null;
  return (
    <Panel title={t("v2.insights.byEvaluator")} end={<span>{t("v2.insights.byEvaluatorSub")}</span>}>
      <div className="v3-ins-bars">
        {summary.byEvaluator.map((e) => (
          <div key={e.evaluatorId} className="v3-ins-bar">
            <span className="name" title={e.evaluatorId}>
              {evaluatorLabel(t, e.evaluatorId)}
            </span>
            <span className="track" aria-hidden="true">
              <i data-s={scoreSignal(e.mean)} style={{ width: `${Math.round((e.mean ?? 0) * 100)}%` }} />
            </span>
            <Score value={e.mean} />
            <span className="meta">
              {t("v2.insights.evalCounts", {
                count: e.count,
                failed: e.failed,
              })}
            </span>
          </div>
        ))}
      </div>
    </Panel>
  );
}

/* ── insight clusters ───────────────────────────────────────────────────── */

const SECTION_SIGNAL: Record<Section, Signal> = {
  failures: "act",
  userIntents: "info",
  executionSummaries: "ok",
};
const SECTION_LABEL: Record<Section, string> = {
  failures: "v2.insights.panel.failures",
  userIntents: "v2.insights.panel.intents",
  executionSummaries: "v2.insights.panel.summaries",
};
const TOP = 6;
const taskLink = (task: V2Task) => `/v2/eval/tasks?view=detail&kind=run&id=${encodeURIComponent(task.id)}`;

function ClusterDialog({ cluster, onClose }: { cluster: MergedCluster; onClose: () => void }) {
  const { t } = useTranslation();
  const tasks = [...new Map(cluster.members.map((m) => [m.task.id, m.task])).values()];
  const subCategories = cluster.members.flatMap((m) => m.cluster.subCategories ?? []);
  const affected = cluster.members.flatMap((m) => m.cluster.affectedSessions ?? []);
  return (
    <Dialog
      wide
      title={cluster.name}
      onClose={onClose}
      foot={
        <Btn kind="ghost" onClick={onClose}>
          {t("v3.insights.close")}
        </Btn>
      }
    >
      <div className="v3-ins-dialog">
        <dl className="v3-kv">
          <dt>{t("v3.insights.kind")}</dt>
          <dd>
            <Chip s={SECTION_SIGNAL[cluster.section]}>{t(SECTION_LABEL[cluster.section])}</Chip>
          </dd>
          <dt>{t("v2.traces.colSession")}</dt>
          <dd>{t("v2.insights.panel.sessions", { count: cluster.sessions })}</dd>
          <dt>{t("v2.insights.panel.sourceTasks")}</dt>
          <dd style={{ display: "grid", gap: 4 }}>
            {tasks.map((task) => (
              <Link key={task.id} to={taskLink(task)} className="v3-ins-link">
                {task.name} · {task.agentName}
              </Link>
            ))}
          </dd>
        </dl>
        {cluster.description && <p className="v3-ins-desc">{cluster.description}</p>}
        {subCategories.length > 0 && (
          <>
            <div className="v3-ins-subtitle">{t("v2.insights.panel.subCategories")}</div>
            {subCategories.map((sub, i) => (
              <div key={i} className="v3-ins-card">
                <b>{sub.name ?? `#${i + 1}`}</b>
                {(sub.rootCauses ?? []).map((cause, j) => (
                  <div key={j} className="v3-ins-cause">
                    <span className="lbl">{t("v2.insights.panel.rootCauses")}</span>
                    {cause.name ?? "—"}
                    {cause.recommendation && (
                      <div className="v3-ins-rec">
                        <span className="lbl">{t("v2.insights.panel.recommendation")}</span>
                        {cause.recommendation}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            ))}
          </>
        )}
        {affected.length > 0 && (
          <>
            <div className="v3-ins-subtitle">
              {t("v2.insights.panel.affected")} · {affected.length}
            </div>
            {affected.slice(0, 20).map((s, i) => (
              <div key={`${s.sessionId}:${i}`} className="v3-ins-card">
                <span className="mono" style={{ color: "var(--v3-text-3)", fontSize: 12 }}>
                  {s.sessionId ?? "—"}
                </span>
                {s.userMessages?.[0] && (
                  <div>
                    <span className="lbl">{t("v2.insights.panel.userMessage")}</span>“{s.userMessages[0]}”
                  </div>
                )}
                {s.approachTaken && (
                  <div>
                    <span className="lbl">{t("v2.insights.panel.approach")}</span>
                    {s.approachTaken}
                  </div>
                )}
                {s.finalOutcome && (
                  <div>
                    <span className="lbl">{t("v2.insights.panel.outcome")}</span>
                    {s.finalOutcome}
                  </div>
                )}
              </div>
            ))}
          </>
        )}
      </div>
    </Dialog>
  );
}

/**
 * The insights runs in scope, their failure / user-intent / execution-summary
 * clusters merged by name, biggest first; a cluster opens its sub-categories, root
 * causes with recommendations, affected sessions and source tasks.
 */
export function ClustersPanel({ tasks, loading, needle }: { tasks: V2Task[]; loading: boolean; needle: string }) {
  const { t } = useTranslation();
  const [section, setSection] = useState<Section>("failures");
  const [expanded, setExpanded] = useState(false);
  const [open, setOpen] = useState<MergedCluster | null>(null);
  const merged = useMemo(() => mergeClusters(tasks), [tasks]);
  const withTrees = tasks.filter((task) => hasInsightTrees(task.run?.insights)).length;
  const failedSessions = useMemo(() => {
    const ids = new Set(merged.failures.flatMap((c) => c.sessionIds));
    return ids.size || merged.failures.reduce((n, c) => n + c.sessions, 0);
  }, [merged]);
  const q = needle.trim().toLowerCase();
  const list = merged[section].filter((c) => !q || `${c.name} ${c.description}`.toLowerCase().includes(q));
  const shown = expanded ? list : list.slice(0, TOP);
  const max = Math.max(1, ...list.map((c) => c.sessions));

  return (
    <>
      <Panel
        title={t("v2.insights.panel.title")}
        signal={merged.failures.length ? "act" : undefined}
        end={
          <Link to="/v2/eval/tasks?view=new" className="v3-btn sm ghost">
            <Plus size={13} /> {t("v2.insights.panel.newTask")}
          </Link>
        }
      >
        <p className="v3-ins-note">{t("v2.insights.panel.sub")}</p>
        {loading && tasks.length === 0 ? (
          <Skeleton rows={3} />
        ) : tasks.length === 0 ? (
          <Empty title={t("v2.insights.panel.empty")} />
        ) : (
          <>
            <div className="v3-ins-istats">
              <Stat
                label={t("v2.insights.panel.tasks")}
                value={tasks.length}
                foot={t("v2.insights.panel.tasksSub", { count: withTrees })}
              />
              <Stat
                label={t("v2.insights.panel.failures")}
                value={merged.failures.length}
                signal={merged.failures.length ? "act" : undefined}
                foot={t("v2.insights.panel.failuresSub", {
                  count: failedSessions,
                })}
              />
              <Stat label={t("v2.insights.panel.intents")} value={merged.userIntents.length} />
              <Stat label={t("v2.insights.panel.summaries")} value={merged.executionSummaries.length} />
            </div>
            {withTrees === 0 ? (
              <p className="v3-ins-note">{t("v2.insights.panel.emptyTrees")}</p>
            ) : (
              <>
                <div style={{ margin: "16px 0 12px" }}>
                  <Filters
                    value={section}
                    onChange={(s) => {
                      setSection(s);
                      setExpanded(false);
                    }}
                    options={SECTIONS.map((s) => ({
                      value: s,
                      label: t(SECTION_LABEL[s]),
                      count: merged[s].length,
                      s: SECTION_SIGNAL[s],
                    }))}
                  />
                </div>
                {list.length === 0 ? (
                  <Empty title={t("v3.insights.noClusters")} />
                ) : (
                  <div className="v3-ins-clusters">
                    {shown.map((c, i) => {
                      const taskCount = new Set(c.members.map((m) => m.task.id)).size;
                      return (
                        <button type="button" key={c.key} className="v3-ins-cluster" onClick={() => setOpen(c)}>
                          <span className="rank" data-s={SECTION_SIGNAL[c.section]}>
                            {i + 1}
                          </span>
                          <span className="body">
                            <b>{c.name}</b>
                            {c.description && <span className="desc">{c.description}</span>}
                            {c.recommendation && (
                              <span className="v3-ins-rec">
                                <span className="lbl">{t("v2.insights.panel.recommendation")}</span>
                                {c.recommendation}
                              </span>
                            )}
                          </span>
                          <span className="meter">
                            <span className="track">
                              <i
                                data-s={SECTION_SIGNAL[c.section]}
                                style={{
                                  width: `${Math.round((c.sessions / max) * 100)}%`,
                                }}
                              />
                            </span>
                            <span className="meta">
                              {t("v2.insights.panel.sessions", {
                                count: c.sessions,
                              })}{" "}
                              ·{" "}
                              {t("v2.insights.panel.inTasks", {
                                count: taskCount,
                              })}
                            </span>
                          </span>
                        </button>
                      );
                    })}
                    {list.length > TOP && (
                      <div>
                        <Btn size="sm" kind="ghost" onClick={() => setExpanded(!expanded)}>
                          {expanded
                            ? t("v2.common.collapse")
                            : t("v2.insights.panel.more", {
                                count: list.length - TOP,
                              })}
                        </Btn>
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
          </>
        )}
      </Panel>
      {/* outside the panel: its reveal transform would pin a fixed overlay inside it */}
      {open && <div className="v3-ins-layer"><ClusterDialog cluster={open} onClose={() => setOpen(null)} /></div>}
    </>
  );
}

/* ── user feedback (thumbs) ─────────────────────────────────────────────── */

const chatLink = (agentId: string, sessionId: string) =>
  `/v3/chat?agent=${encodeURIComponent(agentId)}&session=${encodeURIComponent(sessionId)}`;

/** Thumbs from the console chat and share pages; a thumbs-down is a bad case. */
export function FeedbackPanel({ range }: { range: V2Range }) {
  const { t } = useTranslation();
  const feedback = useLoad(() => feedbackApi.list({ verdict: "down", limit: 50 }), "v3-feedback:down");
  const [adding, setAdding] = useState(false);
  const data = feedback.data;
  const sessions = data?.down_session_ids ?? [];
  const down = data?.counts.down ?? 0;
  return (
    <>
      <Panel
        title={t("feedbackCard.title")}
        signal={down > 0 ? "act" : undefined}
        flush
        end={
          <span style={{ display: "inline-flex", gap: 12, alignItems: "center" }}>
            <span className="v3-ins-thumb" data-s="ok">
              <ThumbsUp size={13} /> {data?.counts.up ?? "—"}
            </span>
            <span className="v3-ins-thumb" data-s={down ? "act" : "off"}>
              <ThumbsDown size={13} /> {data?.counts.down ?? "—"}
            </span>
            <Btn size="sm" disabled={sessions.length === 0} disabledReason={t("story.reason.noDownSessions")} onClick={() => setAdding(true)}>
              <DatabaseZap size={13} /> {t("feedbackCard.toDataset", { count: sessions.length })}
            </Btn>
          </span>
        }
      >
        <p className="v3-ins-note" style={{ padding: "0 20px" }}>
          {t("feedbackCard.desc")}
        </p>
        {feedback.loading && !data ? (
          <div style={{ padding: 20 }}>
            <Skeleton rows={3} />
          </div>
        ) : feedback.error ? (
          <div style={{ padding: 20 }}>
            <Notice s="act"><ApiErrorDetail error={feedback.failure ?? feedback.error} /></Notice>
          </div>
        ) : (data?.items ?? []).length === 0 ? (
          <Empty title={t("feedbackCard.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("feedbackCard.colQuestion")}</th>
                <th>{t("feedbackCard.colAnswer")}</th>
                <th>Agent</th>
                <th>{t("feedbackCard.colSource")}</th>
                <th className="num">{t("v2.insights.colTime")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {(data?.items ?? []).map((r) => (
                <tr key={r.id}>
                  <td style={{ width: 30 }}>
                    {r.verdict === "down" ? (
                      <ThumbsDown size={14} color="var(--v3-act)" />
                    ) : (
                      <ThumbsUp size={14} color="var(--v3-ok)" />
                    )}
                  </td>
                  <td>
                    <span className="v3-ins-clip" title={r.question}>
                      {r.question || "—"}
                    </span>
                    {r.comment && <small className="v3-ins-sub">{r.comment}</small>}
                  </td>
                  <td>
                    <span className="v3-ins-clip" title={r.answer} style={{ color: "var(--v3-text-2)" }}>
                      {r.answer || "—"}
                    </span>
                  </td>
                  <td style={{ color: "var(--v3-text-2)" }}>{r.agent_name}</td>
                  <td>
                    <Chip>{t(`feedbackCard.source.${r.source}`)}</Chip>
                  </td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }} title={fmtTime(r.updated_at)}>
                    {ago(r.updated_at)}
                  </td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}>
                    <Link to={chatLink(r.agent_id, r.session_id)} className="v3-btn sm ghost">
                      {t("feedbackCard.openSession")}
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
      {adding && <div className="v3-ins-layer"><AddToDataset sessionIds={sessions} range={range} onClose={() => setAdding(false)} /></div>}
    </>
  );
}

/* ── per-result table ───────────────────────────────────────────────────── */

function ResultDialog({
  row,
  range,
  onClose,
  onAdd,
}: {
  row: ResultRow;
  range: V2Range;
  onClose: () => void;
  onAdd: () => void;
}) {
  const { t } = useTranslation();
  // transcript only — the full session view's span query is what makes it slow
  const session = useLoad(
    () => (row.sessionId ? api.obsSessionTranscript(row.sessionId, row.agentId) : Promise.resolve(null)),
    `v3-result-transcript:${row.sessionId}:${row.agentId ?? ""}`,
  );
  const turns = session.data?.transcript.turns ?? [];
  return (
    <Dialog
      wide
      title={t("v2.insights.detailTitle")}
      onClose={onClose}
      foot={
        <>
          {row.traceId && (
            <Link
              className="v3-btn ghost"
              to={`/v2/eval/data?tab=traces&view=trace&id=${encodeURIComponent(row.traceId)}&range=${range}`}
            >
              {t("v2.insights.openTrace")}
            </Link>
          )}
          <Btn kind="ghost" onClick={onClose}>
            {t("v3.insights.close")}
          </Btn>
          <Btn kind="primary" disabled={!row.sessionId} disabledReason={t("story.reason.noSessionLinked")} onClick={onAdd}>
            <DatabaseZap size={14} /> {t("v2.insights.addBadCase")}
          </Btn>
        </>
      }
    >
      <div className="v3-ins-dialog">
        <dl className="v3-kv">
          <dt>{t("v2.insights.colOutcome")}</dt>
          <dd>
            <Chip s={OUTCOME_SIGNAL[row.outcome]}>{t(`v2.outcome.${row.outcome}`)}</Chip>
          </dd>
          <dt>{t("v2.insights.colEvaluator")}</dt>
          <dd>{evaluatorLabel(t, row.evaluatorId)}</dd>
          <dt>{t("v2.insights.colRaw")}</dt>
          <dd className="mono">{row.evaluatorId === FAILURE_ANALYSIS_EVALUATOR ? evaluatorScoreLabel(row.score, row.evaluatorId) : (row.score ?? "—")}</dd>
          <dt>{t("v2.insights.colNormalized")}</dt>
          <dd>
            <Score value={row.normalized} />
          </dd>
          <dt>{t("v2.insights.colLabel")}</dt>
          <dd>{row.label ?? "—"}</dd>
          <dt>{t("v2.insights.colTask")}</dt>
          <dd>{row.taskName}</dd>
          <dt>Agent</dt>
          <dd>{row.agent}</dd>
          <dt>{t("v2.traces.colSession")}</dt>
          <dd className="mono">{row.sessionId ?? "—"}</dd>
          <dt>{t("v2.insights.colTime")}</dt>
          <dd>{fmtTime(row.time)}</dd>
        </dl>
        <div className="v3-ins-subtitle">{t("v2.insights.explanation")}</div>
        <pre className="v3-pre">{row.error ?? row.explanation ?? "—"}</pre>
        <div className="v3-ins-subtitle">{t("v2.insights.inputOutput")}</div>
        {!row.sessionId ? (
          <p className="v3-ins-note">{t("v2.traces.noSession")}</p>
        ) : session.loading ? (
          <Skeleton rows={2} />
        ) : turns.length === 0 ? (
          <p className="v3-ins-note">{t("v2.traces.noTranscript")}</p>
        ) : (
          <div style={{ display: "grid", gap: 8 }}>
            {turns.map((turn, i) => {
              const user = turn.role.toLowerCase() === "user";
              return (
                <div key={i} className={user ? "v3-ins-turn user" : "v3-ins-turn"}>
                  <span className="who">{user ? t("v2.traces.user") : "Agent"}</span>
                  <div>{turn.text}</div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </Dialog>
  );
}

const PAGE = 15;

/** Every judged result: export, failed sessions → dataset, and a detail dialog per row. */
export function ResultsPanel({
  rows,
  loading,
  error,
  range,
  exportName,
}: {
  rows: ResultRow[];
  loading: boolean;
  error: string | null;
  range: V2Range;
  exportName: string;
}) {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState<ResultRow | null>(null);
  // the sessions going into a dataset: every failed one, or the one row's
  const [adding, setAdding] = useState<string[] | null>(null);
  const pages = Math.max(1, Math.ceil(rows.length / PAGE));
  const current = Math.min(page, pages);
  const slice = rows.slice((current - 1) * PAGE, current * PAGE);
  // newest first, capped at what one from-sessions call accepts
  const badSessions = useMemo(
    () => [...new Set(rows.filter((r) => r.outcome === "failed" && r.sessionId).map((r) => r.sessionId as string))].slice(0, 50),
    [rows],
  );
  const exportCsv = () =>
    downloadCsv(
      `${exportName}.csv`,
      [
        t("v2.insights.colOutcome"),
        t("v2.insights.colTime"),
        t("v2.insights.colRaw"),
        t("v2.insights.colNormalized"),
        t("v2.insights.colLabel"),
        t("v2.insights.colEvaluator"),
        t("v2.insights.colTask"),
        "Agent",
        t("v2.traces.colSession"),
        t("v2.insights.explanation"),
      ],
      rows.map((r) => [
        t(`v2.outcome.${r.outcome}`),
        fmtTime(r.time),
        r.score,
        r.normalized == null ? null : Number(r.normalized.toFixed(4)),
        r.label,
        evaluatorLabel(t, r.evaluatorId),
        r.taskName,
        r.agent,
        r.sessionId,
        r.error ?? r.explanation,
      ]),
    );

  return (
    <>
      <Panel
        title={t("v2.insights.details")}
        flush
        end={
          <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
            <span className="mono" style={{ color: "var(--v3-text-3)" }}>
              {t("v2.common.total", { count: rows.length })}
            </span>
            <Btn size="sm" disabled={rows.length === 0} disabledReason={t("story.reason.nothingToExport")} onClick={exportCsv}>
              <Download size={13} /> {t("v2.insights.export")}
            </Btn>
            <Btn size="sm" disabled={badSessions.length === 0} disabledReason={t("story.reason.noBadCases")} onClick={() => setAdding(badSessions)}>
              <DatabaseZap size={13} /> {t("v2.insights.badToDataset", { count: badSessions.length })}
            </Btn>
          </span>
        }
      >
        {loading && rows.length === 0 ? (
          <div style={{ padding: 20 }}>
            <Skeleton rows={5} />
          </div>
        ) : error ? (
          <div style={{ padding: 20 }}>
            <Notice s="act">{error}</Notice>
          </div>
        ) : rows.length === 0 ? (
          <Empty title={t("v2.insights.empty")} />
        ) : (
          <>
            <table className="v3-table">
              <thead>
                <tr>
                  <th />
                  <th>{t("v2.insights.colEvaluator")}</th>
                  <th className="num">{t("v2.insights.colNormalized")}</th>
                  <th>{t("v2.insights.explanation")}</th>
                  <th>{t("v2.insights.colTask")}</th>
                  <th>{t("v2.tasks.colSource")}</th>
                  <th className="num">{t("v2.insights.colTime")}</th>
                </tr>
              </thead>
              <tbody>
                {slice.map((r) => (
                  <tr key={r.key} className="click" onClick={() => setOpen(r)}>
                    <td style={{ width: 30 }}>
                      <Lamp s={OUTCOME_SIGNAL[r.outcome]} title={t(`v2.outcome.${r.outcome}`)} />
                    </td>
                    <td>
                      <div className="v3-name">
                        <div>
                          <b>{evaluatorLabel(t, r.evaluatorId)}</b>
                          <small>{r.label ?? t(`v2.outcome.${r.outcome}`)}</small>
                        </div>
                      </div>
                    </td>
                    <td className="num">
                      <Score value={r.normalized} />
                    </td>
                    <td>
                      <span className="v3-ins-clip" title={r.error ?? r.explanation ?? ""} style={{ color: "var(--v3-text-2)" }}>
                        {r.error ?? r.explanation ?? "—"}
                      </span>
                    </td>
                    <td>
                      <div className="v3-name">
                        <div>
                          <b style={{ fontWeight: 500 }}>{r.taskName}</b>
                          <small>{r.agent}</small>
                        </div>
                      </div>
                    </td>
                    <td>
                      <Chip>{t(`v2.taskSource.${r.source}Short`)}</Chip>
                    </td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }} title={fmtTime(r.time)}>
                      {ago(r.time)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {pages > 1 && (
              <div className="v3-ins-pager">
                <Btn size="sm" kind="ghost" disabled={current <= 1} disabledReason={t("story.reason.firstPage")} onClick={() => setPage(current - 1)}>
                  ‹
                </Btn>
                <span className="mono">
                  {current} / {pages}
                </span>
                <Btn size="sm" kind="ghost" disabled={current >= pages} disabledReason={t("story.reason.lastPage")} onClick={() => setPage(current + 1)}>
                  ›
                </Btn>
              </div>
            )}
          </>
        )}
      </Panel>
      {open && (
        <div className="v3-ins-layer">
        <ResultDialog
          row={open}
          range={range}
          onClose={() => setOpen(null)}
          onAdd={() => {
            const sid = open.sessionId;
            setOpen(null);
            if (sid) setAdding([sid]);
          }}
        />
        </div>
      )}
      {adding && <div className="v3-ins-layer"><AddToDataset sessionIds={adding} range={range} onClose={() => setAdding(null)} /></div>}
    </>
  );
}

import { DatabaseZap, Download } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { api, type V2Range } from "../lib/api";
import { evaluatorLabel } from "../lib/evaluators";
import { downloadCsv, fmtScore, fmtTime, scoreTone } from "./format";
import { useLoad, usePaged } from "./hooks";
import { AddToDatasetModal } from "./pages/data/AddToDataset";
import type { ResultRow, ResultSummary } from "./results";
import {
  Button,
  Card,
  type Column,
  Descriptions,
  Drawer,
  Kpi,
  LinkButton,
  Pager,
  Segmented,
  Spin,
  Table,
  Tag,
} from "./ui";

export function OutcomeTag({ outcome }: { outcome: ResultRow["outcome"] }) {
  const { t } = useTranslation();
  const tone = outcome === "passed" ? "green" : outcome === "failed" ? "red" : "orange";
  return <Tag tone={tone}>{t(`v2.outcome.${outcome}`)}</Tag>;
}

export function Score({ value }: { value: number | null }) {
  if (value == null) return <span className="v2-muted">—</span>;
  return <span className={`v2-score ${scoreTone(value)}`}>{fmtScore(value)}</span>;
}

/** KPI strip: count · normalized mean · Bad Case (the insights dashboard head). */
export function SummaryKpis({ summary }: { summary: ResultSummary }) {
  const { t } = useTranslation();
  return (
    <div className="v2-kpis">
      <Kpi
        label={t("v2.insights.kpiCount")}
        value={summary.total}
        sub={t("v2.insights.kpiCountSub", { passed: summary.passed, failed: summary.failed, errors: summary.errors })}
        testId="v2-kpi-count"
      />
      <Kpi
        label={t("v2.insights.kpiMean")}
        value={summary.mean == null ? "—" : summary.mean.toFixed(2)}
        tone={summary.mean == null ? undefined : summary.mean >= 0.7 ? "good" : "bad"}
        sub={t("v2.insights.kpiMeanSub")}
        testId="v2-kpi-mean"
      />
      <Kpi
        label="Bad Case"
        value={summary.failed}
        tone={summary.failed > 0 ? "bad" : undefined}
        sub={t("v2.insights.kpiBadSub")}
        testId="v2-kpi-bad"
      />
      <Kpi
        label={t("v2.insights.kpiPassRate")}
        value={summary.total - summary.errors > 0 ? `${Math.round((summary.passed / (summary.total - summary.errors)) * 100)}%` : "—"}
        sub={t("v2.insights.kpiPassRateSub")}
      />
    </div>
  );
}

/** Per-evaluator mean bars, weakest first. */
export function EvaluatorBreakdown({ summary }: { summary: ResultSummary }) {
  const { t } = useTranslation();
  if (summary.byEvaluator.length === 0) return null;
  return (
    <Card title={t("v2.insights.byEvaluator")} sub={t("v2.insights.byEvaluatorSub")}>
      <div className="v2-stack">
        {summary.byEvaluator.map((e) => (
          <div key={e.evaluatorId} className="v2-row" style={{ flexWrap: "nowrap" }}>
            <span style={{ width: 220, flex: "none" }} className="clip" title={e.evaluatorId}>
              {evaluatorLabel(t, e.evaluatorId)}
            </span>
            <div className="v2-bar" style={{ flex: 1 }}>
              <span
                style={{
                  width: `${Math.round((e.mean ?? 0) * 100)}%`,
                  background: e.mean == null ? "var(--v2-ink-4)" : e.mean >= 0.7 ? "var(--v2-success)" : e.mean >= 0.4 ? "var(--v2-warning)" : "var(--v2-danger)",
                }}
              />
            </div>
            <span style={{ width: 48, textAlign: "right" }}>
              <Score value={e.mean} />
            </span>
            <span className="v2-muted" style={{ width: 150, textAlign: "right", fontSize: 13 }}>
              {t("v2.insights.evalCounts", { count: e.count, failed: e.failed })}
            </span>
          </div>
        ))}
      </div>
    </Card>
  );
}

function ResultDrawer({ row, range, onClose }: { row: ResultRow; range: V2Range; onClose: () => void }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const session = useLoad(
    () => (row.sessionId ? api.obsSession(row.sessionId, range) : Promise.resolve(null)),
    `result-session:${row.sessionId}:${range}`,
  );
  const [adding, setAdding] = useState(false);
  const turns = session.data?.transcript.turns ?? [];
  return (
    <Drawer
      open
      title={t("v2.insights.detailTitle")}
      onClose={onClose}
      testId="v2-result-drawer"
      footer={
        <>
          {row.traceId && (
            <Button onClick={() => navigate(`/v2/eval/data?tab=traces&view=trace&id=${row.traceId}&range=${range}`)}>
              {t("v2.insights.openTrace")}
            </Button>
          )}
          <Button kind="primary" disabled={!row.sessionId} onClick={() => setAdding(true)}>
            <DatabaseZap size={14} aria-hidden="true" />
            {t("v2.insights.addBadCase")}
          </Button>
        </>
      }
    >
      <div className="v2-stack" style={{ gap: 16 }}>
        <Descriptions
          one
          items={[
            { label: t("v2.insights.colOutcome"), value: <OutcomeTag outcome={row.outcome} /> },
            { label: t("v2.insights.colEvaluator"), value: evaluatorLabel(t, row.evaluatorId) },
            { label: t("v2.insights.colRaw"), value: row.score == null ? "—" : row.score },
            { label: t("v2.insights.colNormalized"), value: <Score value={row.normalized} /> },
            { label: t("v2.insights.colLabel"), value: row.label ?? "—" },
            { label: t("v2.insights.colTask"), value: row.taskName },
            { label: "Agent", value: row.agent },
            { label: t("v2.traces.colSession"), value: row.sessionId ? <span className="mono">{row.sessionId}</span> : "—" },
            { label: t("v2.insights.colTime"), value: fmtTime(row.time) },
          ]}
        />
        <div>
          <h3 className="v2-sec-title" style={{ fontSize: 14 }}>
            {t("v2.insights.explanation")}
          </h3>
          <pre className="v2-pre">{row.error ?? row.explanation ?? "—"}</pre>
        </div>
        <div>
          <h3 className="v2-sec-title" style={{ fontSize: 14 }}>
            {t("v2.insights.inputOutput")}
          </h3>
          {!row.sessionId ? (
            <p className="v2-muted">{t("v2.traces.noSession")}</p>
          ) : session.loading ? (
            <Spin />
          ) : turns.length === 0 ? (
            <p className="v2-muted">{t("v2.traces.noTranscript")}</p>
          ) : (
            turns.map((turn, i) => (
              <div key={i} className={turn.role.toLowerCase() === "user" ? "v2-turn user" : "v2-turn"}>
                <span className="who">{turn.role.toLowerCase() === "user" ? t("v2.traces.user") : "Agent"}</span>
                <div className="msg">{turn.text}</div>
              </div>
            ))
          )}
        </div>
      </div>
      <AddToDatasetModal open={adding} sessionIds={row.sessionId ? [row.sessionId] : []} range={range} onClose={() => setAdding(false)} />
    </Drawer>
  );
}

/**
 * The per-result table: outcome · time · raw / normalized score · label ·
 * evaluator · task · agent · source · session, with density switch, CSV export
 * and a detail drawer (judge explanation + the session's input/output).
 */
export function ResultsTable({
  rows,
  loading,
  error,
  onRetry,
  range,
  showTask = true,
  exportName,
}: {
  rows: ResultRow[];
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  range: V2Range;
  showTask?: boolean;
  exportName: string;
}) {
  const { t } = useTranslation();
  const [density, setDensity] = useState<"dense" | "default" | "loose">("default");
  const [open, setOpen] = useState<ResultRow | null>(null);
  const [adding, setAdding] = useState(false);
  const paged = usePaged(rows, 15);
  const badSessions = useMemo(
    // newest first, capped at what one from-sessions call accepts
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

  const columns: Column<ResultRow>[] = [
    { key: "outcome", title: t("v2.insights.colOutcome"), render: (r) => <OutcomeTag outcome={r.outcome} /> },
    { key: "time", title: t("v2.insights.colTime"), className: "nowrap", render: (r) => fmtTime(r.time) },
    { key: "raw", title: t("v2.insights.colRaw"), className: "num", render: (r) => (r.score == null ? "—" : r.score) },
    { key: "norm", title: t("v2.insights.colNormalized"), render: (r) => <Score value={r.normalized} /> },
    { key: "label", title: t("v2.insights.colLabel"), render: (r) => (r.label ? <Tag tone="outline">{r.label}</Tag> : "—") },
    { key: "explanation", title: t("v2.insights.explanation"), render: (r) => <span className="clip" title={r.error ?? r.explanation ?? ""}>{r.error ?? r.explanation ?? "—"}</span> },
    { key: "evaluator", title: t("v2.insights.colEvaluator"), render: (r) => evaluatorLabel(t, r.evaluatorId) },
    ...(showTask
      ? [
          { key: "task", title: t("v2.insights.colTask"), render: (r: ResultRow) => r.taskName },
          { key: "agent", title: "Agent", render: (r: ResultRow) => r.agent },
          { key: "source", title: t("v2.tasks.colSource"), render: (r: ResultRow) => t(`v2.taskSource.${r.source}Short`) },
        ]
      : []),
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (r) => <LinkButton onClick={() => setOpen(r)}>{t("v2.common.detail")}</LinkButton>,
    },
  ];

  return (
    <>
      <div className="v2-toolbar">
        <Button disabled={rows.length === 0} onClick={exportCsv} testId="v2-results-export">
          <Download size={14} aria-hidden="true" />
          {t("v2.insights.export")}
        </Button>
        <Button disabled={badSessions.length === 0} onClick={() => setAdding(true)} testId="v2-results-badcases">
          <DatabaseZap size={14} aria-hidden="true" />
          {t("v2.insights.badToDataset", { count: badSessions.length })}
        </Button>
        <div className="end">
          <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
          <Segmented
            value={density}
            onChange={setDensity}
            options={[
              { value: "dense", label: t("v2.insights.dense") },
              { value: "default", label: t("v2.insights.default") },
              { value: "loose", label: t("v2.insights.loose") },
            ]}
          />
        </div>
      </div>
      <Table
        columns={columns}
        rows={paged.slice}
        rowKey={(r) => r.key}
        loading={loading}
        error={error}
        onRetry={onRetry}
        density={density}
        empty={t("v2.insights.empty")}
        testId="v2-results-table"
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      {open && <ResultDrawer row={open} range={range} onClose={() => setOpen(null)} />}
      <AddToDatasetModal open={adding} sessionIds={badSessions} range={range} onClose={() => setAdding(false)} />
    </>
  );
}

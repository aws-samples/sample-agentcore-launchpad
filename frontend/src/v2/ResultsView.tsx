import { DatabaseZap, Download } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { api, type V2Range } from "../lib/api";
import type { EvaluationScore } from "../lib/evaluation";
import { evaluatorLabel, passThreshold } from "../lib/evaluators";
import { ExtractionRowFlag } from "./ExtractionCheck";
import { ScaleLabel } from "./ScaleLabel";
import { downloadCsv, fmtScore, fmtTime, normalizedInterval, scoreTone } from "./format";
import { useLoad, usePaged } from "./hooks";
import { AddToDatasetModal } from "./pages/data/AddToDataset";
import {
  filterResults,
  type ResultRow,
  type ResultSort,
  type ResultSortKey,
  type ResultSummary,
  sortResults,
} from "./results";
import {
  Button,
  Card,
  type Column,
  Descriptions,
  Drawer,
  FilterSelect,
  Kpi,
  LinkButton,
  Pager,
  SearchInput,
  Segmented,
  Spin,
  Table,
  Tag,
} from "./ui";
import { useEvaluatorNames } from "./useEvaluatorNames";
import { EvaluatorName } from "./EvaluatorName";

/** A row's outcome; with `evaluatorId` the tag's tooltip states that evaluator's own pass bar
 *  (N17-obs431: the bar is per evaluator — 0.7 by default, 1.0 for Faithfulness & co.). */
export function OutcomeTag({ outcome, evaluatorId }: { outcome: ResultRow["outcome"]; evaluatorId?: string }) {
  const { t } = useTranslation();
  const tone = outcome === "passed" ? "green" : outcome === "failed" ? "red" : "orange";
  const title = evaluatorId && outcome !== "error" ? t("v2.outcome.barTitle", { bar: fmtScore(passThreshold(evaluatorId)) }) : undefined;
  return (
    <Tag tone={tone} title={title}>
      {t(`v2.outcome.${outcome}`)}
    </Tag>
  );
}

/** A normalized score; `evaluatorId` (one judged row) colours it by that evaluator's pass bar. */
export function Score({ value, evaluatorId }: { value: number | null; evaluatorId?: string }) {
  if (value == null) return <span className="v2-muted">—</span>;
  return <span className={`v2-score ${scoreTone(value, evaluatorId)}`}>{fmtScore(value)}</span>;
}

/** KPI strip: count · normalized mean · Bad Case (the insights dashboard head). */
export function SummaryKpis({ summary, flaggedSessions = 0 }: { summary: ResultSummary; flaggedSessions?: number }) {
  const { t } = useTranslation();
  return (
    <div className="v2-kpis">
      {flaggedSessions > 0 && (
        <Kpi
          label={t("v2.extraction.kpiLabel")}
          value={flaggedSessions}
          tone="bad"
          sub={t("v2.extraction.kpiSub")}
          testId="v2-kpi-extraction"
        />
      )}
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
        label={t("v2.outcome.failed")}
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

/** EVAL-TRUST 3.2: "n = 12 sessions · 95 % CI 0.61–0.83" of one evaluator's run score — on the
 *  normalized scale the row's mean uses (N17-obs430: a penalty evaluator's interval is mirrored). */
function ConfidenceLine({ score }: { score: EvaluationScore | undefined }) {
  const { t } = useTranslation();
  if (!score || score.sessions_n == null) return null;
  const ci = normalizedInterval(score);
  return (
    <span className="v2-muted" style={{ width: 210, textAlign: "right", fontSize: 12 }} data-testid={`v2-ci-${score.evaluatorId}`}>
      {ci
        ? t("v2.insights.confidence", { n: score.sessions_n, lo: ci[0].toFixed(2), hi: ci[1].toFixed(2) })
        : t("v2.insights.confidenceNone", { n: score.sessions_n })}
    </span>
  );
}

/** Per-evaluator mean bars, weakest first; with a run's `scores`, each row also states its
 *  sample size and 95 % interval. */
export function EvaluatorBreakdown({ summary, scores }: { summary: ResultSummary; scores?: EvaluationScore[] }) {
  const { t } = useTranslation();
  useEvaluatorNames(); // N17-obs429: custom evaluators by name
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
            {scores && <ConfidenceLine score={scores.find((s) => s.evaluatorId === e.evaluatorId)} />}
          </div>
        ))}
      </div>
    </Card>
  );
}

function ResultDrawer({ row, range, onClose }: { row: ResultRow; range: V2Range; onClose: () => void }) {
  const { t } = useTranslation();
  useEvaluatorNames(); // N17-obs429: custom evaluators by name
  const navigate = useNavigate();
  // Transcript only — the full session view's 7-day span query is what made
  // this drawer slow, and it shows nothing from it.
  const session = useLoad(
    () => (row.sessionId ? api.obsSessionTranscript(row.sessionId, row.agentId) : Promise.resolve(null)),
    `result-transcript:${row.sessionId}:${row.agentId ?? ""}`,
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
            { label: t("v2.insights.colOutcome"), value: <OutcomeTag outcome={row.outcome} evaluatorId={row.evaluatorId} /> },
            { label: t("v2.insights.colEvaluator"), value: <EvaluatorName id={row.evaluatorId} /> },
            { label: t("v2.insights.colRaw"), value: row.score == null ? "—" : row.score },
            { label: t("v2.insights.colNormalized"), value: <Score value={row.normalized} evaluatorId={row.evaluatorId} /> },
            { label: t("v2.insights.colLabel"), value: row.label ? <ScaleLabel evaluatorId={row.evaluatorId} label={row.label} /> : "—" },
            { label: t("v2.insights.colTask"), value: row.taskName },
            { label: "Agent", value: row.agent },
            { label: t("v2.traces.colSession"), value: row.sessionId ? <span className="mono">{row.sessionId}</span> : "—" },
            {
              label: t("v2.insights.colScenario"),
              value: row.scenarioId ? `${row.scenarioId}${row.scenarioLabel ? ` · ${row.scenarioLabel}` : ""}` : "—",
            },
            { label: t("v2.insights.colTrace"), value: row.traceId ? <span className="mono">{row.traceId}</span> : "—" },
            {
              label: t("v2.insights.colTool"),
              value: row.toolName ? (
                <span className="mono">
                  {row.toolName}
                  {row.spanId ? ` · ${row.spanId}` : ""}
                </span>
              ) : (
                "—"
              ),
            },
            {
              label: t("v2.insights.colExtraction"),
              value: row.extractionStatus
                ? t(`v2.extractionStatus.${row.extractionStatus}`, { defaultValue: row.extractionStatus })
                : "—",
            },
            { label: t("v2.insights.colJudge"), value: row.judgeModelId ?? "—" },
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
 * evaluator · task · agent · source · session, with sortable columns, density
 * switch, CSV export and a detail drawer (judge explanation + the session's
 * input/output). `filterable` adds evaluator / outcome / score-band filters and
 * a search, for a page that has no filters of its own; export and "Bad Case →
 * dataset" act on what the filters leave.
 */
export function ResultsTable({
  rows,
  loading,
  error,
  onRetry,
  range,
  showTask = true,
  filterable = false,
  exportName,
}: {
  rows: ResultRow[];
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  range: V2Range;
  showTask?: boolean;
  filterable?: boolean;
  exportName: string;
}) {
  const { t } = useTranslation();
  useEvaluatorNames(); // N17-obs429: custom evaluators by name
  const [density, setDensity] = useState<"dense" | "default" | "loose">("default");
  const [open, setOpen] = useState<ResultRow | null>(null);
  const [adding, setAdding] = useState(false);
  const [evaluator, setEvaluator] = useState("");
  const [outcome, setOutcome] = useState("");
  const [band, setBand] = useState("");
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<ResultSort | null>(null);
  const evaluators = useMemo(() => [...new Set(rows.map((r) => r.evaluatorId))], [rows]);
  const shown = useMemo(
    () => sortResults(filterable ? filterResults(rows, { evaluator, outcome, band, q }) : rows, sort),
    [rows, filterable, evaluator, outcome, band, q, sort],
  );
  const paged = usePaged(shown, 15);
  const narrowed = shown.length !== rows.length;
  // any change of filter or order starts again from the first page
  const refine =
    <V,>(set: (value: V) => void) =>
    (value: V) => {
      set(value);
      paged.setPage(1);
    };
  // asc → desc → the incoming order
  const toggleSort = refine((key: string) =>
    setSort((cur) => (cur?.key !== key ? { key: key as ResultSortKey, dir: "asc" } : cur.dir === "asc" ? { ...cur, dir: "desc" } : null)),
  );
  const badSessions = useMemo(
    // N17-obs433: distinct SESSIONS, in the table's current order (filters and sort apply),
    // capped at what one from-sessions call accepts — one dataset case per session at most
    () => [...new Set(shown.filter((r) => r.outcome === "failed" && r.sessionId).map((r) => r.sessionId as string))].slice(0, 50),
    [shown],
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
        t("v2.insights.colTrace"),
        t("v2.insights.colTool"),
        t("v2.insights.colExtraction"),
        t("v2.insights.colJudge"),
        t("v2.insights.explanation"),
      ],
      shown.map((r) => [
        t(`v2.outcome.${r.outcome}`) + (r.extractionMismatch ? ` · ${t("v2.extraction.rowFlag")}` : ""),
        fmtTime(r.time),
        r.score,
        r.normalized == null ? null : Number(r.normalized.toFixed(4)),
        r.label,
        evaluatorLabel(t, r.evaluatorId),
        r.taskName,
        r.agent,
        r.sessionId,
        r.traceId,
        r.toolName ? `${r.toolName}${r.spanId ? ` (${r.spanId})` : ""}` : null,
        r.extractionStatus ?? null,
        r.judgeModelId ?? null,
        r.error ?? r.explanation,
      ]),
    );

  const columns: Column<ResultRow>[] = [
    {
      key: "outcome",
      title: t("v2.insights.colOutcome"),
      sortable: true,
      render: (r) => (
        <>
          <OutcomeTag outcome={r.outcome} evaluatorId={r.evaluatorId} />
          {r.extractionMismatch && <ExtractionRowFlag />}
          {r.failureClass && (
            <Tag tone="orange" testId={`v2-failure-class-${r.key}`}>{t(`v2.unitCoverage.class.${r.failureClass}`)}</Tag>
          )}
        </>
      ),
    },
    { key: "time", title: t("v2.insights.colTime"), className: "nowrap", sortable: true, render: (r) => fmtTime(r.time) },
    { key: "raw", title: t("v2.insights.colRaw"), className: "num", sortable: true, render: (r) => (r.score == null ? "—" : r.score) },
    { key: "norm", title: t("v2.insights.colNormalized"), sortable: true, render: (r) => <Score value={r.normalized} evaluatorId={r.evaluatorId} /> },
    { key: "label", title: t("v2.insights.colLabel"), render: (r) => (r.label ? <Tag tone="outline"><ScaleLabel evaluatorId={r.evaluatorId} label={r.label} /></Tag> : "—") },
    { key: "explanation", title: t("v2.insights.explanation"), render: (r) => <span className="clip" title={r.error ?? r.explanation ?? ""}>{r.error ?? r.explanation ?? "—"}</span> },
    { key: "evaluator", title: t("v2.insights.colEvaluator"), sortable: true, render: (r) => <EvaluatorName id={r.evaluatorId} /> },
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
      {filterable && (
        <div className="v2-toolbar">
          <FilterSelect
            label={t("v2.insights.colEvaluator")}
            value={evaluator}
            allLabel={t("v2.common.all")}
            onChange={refine(setEvaluator)}
            options={evaluators.map((id) => ({ value: id, label: evaluatorLabel(t, id) }))}
            testId="v2-results-filter-evaluator"
          />
          <FilterSelect
            label={t("v2.insights.colOutcome")}
            value={outcome}
            allLabel={t("v2.common.all")}
            onChange={refine(setOutcome)}
            options={(["passed", "failed", "error"] as const).map((o) => ({ value: o, label: t(`v2.outcome.${o}`) }))}
            testId="v2-results-filter-outcome"
          />
          <FilterSelect
            label={t("v2.insights.scoreBand")}
            value={band}
            allLabel={t("v2.common.all")}
            onChange={refine(setBand)}
            options={(["low", "mid", "high"] as const).map((b) => ({ value: b, label: t(`v2.insights.band.${b}`) }))}
            testId="v2-results-filter-band"
          />
          <div className="end">
            <SearchInput value={q} onChange={refine(setQ)} placeholder={t("v2.insights.search")} testId="v2-results-search" />
          </div>
        </div>
      )}
      <div className="v2-toolbar">
        <Button disabled={shown.length === 0} onClick={exportCsv} testId="v2-results-export">
          <Download size={14} aria-hidden="true" />
          {t("v2.insights.export")}
        </Button>
        <Button
          disabled={badSessions.length === 0}
          onClick={() => setAdding(true)}
          title={t("v2.insights.badToDatasetHint")}
          testId="v2-results-badcases"
        >
          <DatabaseZap size={14} aria-hidden="true" />
          {t("v2.insights.badToDataset", { count: badSessions.length })}
        </Button>
        <div className="end">
          <span className="v2-count">
            {narrowed ? t("v2.insights.filteredCount", { count: shown.length, total: rows.length }) : t("v2.common.total", { count: rows.length })}
          </span>
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
        sort={sort}
        onSort={toggleSort}
        empty={narrowed ? t("v2.insights.emptyFiltered") : t("v2.insights.empty")}
        testId="v2-results-table"
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      {open && <ResultDrawer row={open} range={range} onClose={() => setOpen(null)} />}
      <AddToDatasetModal open={adding} sessionIds={badSessions} range={range} onClose={() => setAdding(false)} />
    </>
  );
}

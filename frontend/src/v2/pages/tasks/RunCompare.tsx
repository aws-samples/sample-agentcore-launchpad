import type { TFunction } from "i18next";
import { ArrowLeftRight, ArrowRight, ChevronDown, ChevronRight, DatabaseZap } from "lucide-react";
import { Fragment, type ReactNode, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import {
  api,
  type RunBenchmark,
  type RunCompare as RunCompareData,
  type RunCompareRun,
  type RunCompareSide,
  type SnapshotDiff,
} from "../../../lib/api";
import { evaluatorLabel } from "../../../lib/evaluators";
import {
  agentKeyOf,
  type AssessedCase,
  assessCases,
  basisEvaluators,
  type CaseClass,
  caseColumns,
  type CaseCounts,
  caseCounts,
  type CaseFilter,
  classify,
  type CompareNotice,
  COMPOSITE_BASIS,
  filterCases,
  notices as compareNotices,
  regressedSessions,
  runComposite,
  runScore,
  sharedEvaluators,
  sideScore,
  sortCases,
} from "../../compare";
import { creatorLabel, fmtTime, normalizedScore } from "../../format";
import { useLoad, usePaged } from "../../hooks";
import { Score } from "../../ResultsView";
import { ScaleLabel } from "../../ScaleLabel";
import { sourceLabel, STATUS_TONE, statusLabel, taskFromRun } from "../../tasks";
import { Alert, Button, Card, FilterSelect, FlowHeader, Kpi, LinkButton, Pager, Segmented, Spin, Tag, type TagTone } from "../../ui";
import { DiffBody } from "../agents/SnapshotsCard";
import { noteLabel } from "../agents/snapshots";
import { AddToDatasetModal } from "../data/AddToDataset";
import { combined, scoreEntry, type Significance, significance } from "../../significance";
import { DeltaChip, ScorePair, VersionTag } from "./scorecardUi";
import { useEvaluatorNames } from "../../useEvaluatorNames";
import { EvaluatorName } from "../../EvaluatorName";

const CASE_PAGE = 20;
/** Both diff sides are snapshots, so DiffBody never reads the agent's current spec
 *  (it only does for a `"current"` side) and the page needs no agent read for it. */
const NO_CURRENT_SPEC: Record<string, unknown> = {};

const SIDES = ["a", "b"] as const;
type Side = (typeof SIDES)[number];
const SIDE_LABEL: Record<Side, string> = { a: "A", b: "B" };

const CLASS_TONE: Record<CaseClass, TagTone> = {
  down: "red",
  up: "green",
  same: "gray",
  unscored: "gray",
  onlyA: "outline",
  onlyB: "outline",
};

/** An in-app entry to go back to: the router keeps `idx` in the history state (0 on the
 *  first entry; a replace keeps it), so a deep link never navigates out of the console. */
function hasHistory(): boolean {
  const idx = (window.history.state as { idx?: unknown } | null)?.idx;
  return typeof idx === "number" && idx > 0;
}

function joined(t: TFunction, ids: string[]): string {
  return ids.map((id) => evaluatorLabel(t, id)).join(t("v2.compare.sep"));
}

function benchName(t: TFunction, bench: RunBenchmark): string {
  if (bench.kind === "live") return t("v2.compare.live");
  if (bench.hidden) return t("v2.common.hiddenRef"); // N15-obs15: a hidden dataset, never its id
  return bench.label || bench.dataset_id || "—";
}

/**
 * Two finished evaluator runs side by side (`?view=compare&a=&b=`): overall deltas per
 * evaluator, the cases that got better or worse with both sides' judges, and the
 * agent's configuration change between the two versions. A is the earlier run by
 * convention, but the page shows whatever it is given.
 */
export function RunCompare({ a, b }: { a: string; b: string }) {
  const { t } = useTranslation();
  useEvaluatorNames(); // N17-obs429: custom evaluators by name
  const navigate = useNavigate();
  const [, setParams] = useSearchParams();
  const cmp = useLoad<RunCompareData>(() => api.compareEvaluationRuns(a, b), `run-compare:${a}:${b}`);
  const [basis, setBasis] = useState<string>(COMPOSITE_BASIS);
  const [adding, setAdding] = useState(false);
  const data = cmp.data;

  const shared = useMemo(() => (data ? sharedEvaluators(data.runs.a, data.runs.b) : []), [data]);
  // the per-case view needs both sides' judge records; without one, KPIs still show
  const readable = !!data && data.alignment !== "none" && !!data.results?.a.available && !!data.results?.b.available;
  const assessed = useMemo(
    () => (data && readable ? assessCases(data.cases, basisEvaluators(basis, shared)) : []),
    [data, readable, basis, shared],
  );
  const counts = useMemo(() => caseCounts(assessed), [assessed]);
  const regressed = useMemo(() => regressedSessions(assessed), [assessed]);
  // a selection no longer shared reads as the composite, as `basisEvaluators` does
  const basisLabel = basis !== COMPOSITE_BASIS && shared.includes(basis) ? evaluatorLabel(t, basis) : t("v2.compare.composite");

  const back = () => (hasHistory() ? navigate(-1) : setParams({}));
  // a view toggle, not a step: replace, so Back still returns to where the user came from
  const swap = () => setParams({ view: "compare", a: b, b: a }, { replace: true });
  const sameAgent = !!data && agentKeyOf(data.runs.a) === agentKeyOf(data.runs.b);

  return (
    <div className="v2-compare" data-testid="v2-compare">
      <FlowHeader
        title={
          <span className="v2-row">
            {t("v2.compare.title")}
            {data && sameAgent && <span className="v2-compare-agent">{data.runs.b.agent_name}</span>}
          </span>
        }
        onBack={back}
        end={
          <>
            <Button onClick={swap} testId="v2-compare-swap">
              <ArrowLeftRight size={14} aria-hidden="true" />
              {t("v2.compare.swap")}
            </Button>
            <Button
              disabled={!regressed.length}
              title={
                regressed.length
                  ? t("v2.compare.addRegressedHint", { count: regressed.length })
                  : t(readable ? "v2.compare.addRegressedNone" : "v2.compare.addRegressedUnaligned")
              }
              onClick={() => setAdding(true)}
              testId="v2-compare-add-dataset"
            >
              <DatabaseZap size={14} aria-hidden="true" />
              {t("v2.compare.addRegressed")}
            </Button>
          </>
        }
      />
      {cmp.loading && !data ? (
        <Spin />
      ) : cmp.error && !data ? (
        <Alert tone="error" action={<LinkButton onClick={cmp.reload}>{t("v2.common.retry")}</LinkButton>}>
          {cmp.error}
        </Alert>
      ) : data ? (
        <>
          <div className="v2-compare-pair">
            <RunCard side="a" run={data.runs.a} showAgent={!sameAgent} />
            <span className="to" aria-hidden="true">
              <ArrowRight size={18} />
            </span>
            <RunCard side="b" run={data.runs.b} showAgent={!sameAgent} />
          </div>
          <NoticeList cmp={data} />
          <KpiRow cmp={data} shared={shared} counts={readable ? counts : null} basisLabel={basisLabel} />
          <ConfigCard cmp={data} />
          {data.alignment !== "none" && (
            <CasesCard
              cmp={data}
              shared={shared}
              basis={basis}
              onBasis={setBasis}
              assessed={assessed}
              counts={counts}
            />
          )}
        </>
      ) : null}
      {/* "7d", as the task detail's Bad Case → dataset: the widest window the reader takes */}
      <AddToDatasetModal open={adding} sessionIds={regressed} range="7d" onClose={() => setAdding(false)} />
    </div>
  );
}

function RunCard({ side, run, showAgent }: { side: Side; run: RunCompareRun; showAgent: boolean }) {
  const { t } = useTranslation();
  useEvaluatorNames(); // N17-obs429: custom evaluators by name
  const [, setParams] = useSearchParams();
  const task = taskFromRun(run);
  const pinned = run.dataset_version ? ` · v${run.dataset_version}` : "";
  return (
    <Card testId={`v2-compare-card-${side}`}>
      <div className="v2-compare-run">
        <div className="h">
          <span className="v2-compare-ab">{SIDE_LABEL[side]}</span>
          <span className="name" title={task.name}>
            {task.name}
          </span>
          <VersionTag snapshot={run.snapshot} testId={`v2-compare-version-${side}`} />
          {task.status !== "completed" && <Tag tone={STATUS_TONE[task.status]}>{statusLabel(t, task.status)}</Tag>}
        </div>
        <span className="m">
          {showAgent && `${run.agent_name} · `}
          {fmtTime(run.created_at)} · {creatorLabel(run)}
        </span>
        <span className="m">
          {sourceLabel(t, task)}
          {pinned} · {t("v2.compare.sessions", { count: run.session_ids.length })}
        </span>
        <span className="m">{joined(t, run.evaluators) || "—"}</span>
        <span>
          <LinkButton onClick={() => setParams({ view: "detail", kind: "run", id: run.id })} testId={`v2-compare-open-${side}`}>
            {t("v2.compare.openRun")}
          </LinkButton>
        </span>
      </div>
    </Card>
  );
}

function noticeText(t: TFunction, notice: CompareNotice, cmp: RunCompareData): string {
  const { a, b } = cmp.runs;
  switch (notice.kind) {
    case "scenario":
      return t("v2.compare.notice.scenario", { name: benchName(t, b.benchmark), count: notice.shared ?? 0 });
    case "order":
      return t("v2.compare.notice.order", { count: notice.shared ?? 0 });
    case "revision":
      // both revisions may still hold the same scenario ids (an edited case): no "0 more"
      return notice.only
        ? t("v2.compare.notice.revision", { shared: notice.shared ?? 0, only: notice.only })
        : t("v2.compare.notice.revisionAll", { shared: notice.shared ?? 0 });
    case "partial":
      return t("v2.compare.notice.partial", { count: notice.only ?? 0 });
    case "live":
      return t("v2.compare.notice.live");
    case "benchmark":
      return t("v2.compare.notice.benchmark", { a: benchName(t, a.benchmark), b: benchName(t, b.benchmark) });
    case "unaligned":
      return t("v2.compare.notice.unaligned", {
        why: t(`v2.compare.unaligned.${notice.reason ?? "unaligned"}`, { defaultValue: t("v2.compare.unaligned.unaligned") }),
      });
    case "agents":
      return t("v2.compare.notice.agents", { a: a.agent_name, b: b.agent_name });
    case "evaluators":
      return t("v2.compare.notice.evaluators", { list: joined(t, notice.evaluators ?? []) });
    case "judges":
      return t("v2.compare.notice.judges", { a: cmp.judges?.a ?? "—", b: cmp.judges?.b ?? "—" });
  }
}

function NoticeList({ cmp }: { cmp: RunCompareData }) {
  const { t } = useTranslation();
  return (
    <div className="v2-compare-notices" data-testid="v2-compare-notice">
      {compareNotices(cmp).map((notice) => (
        <div key={notice.kind} data-testid={`v2-compare-notice-${notice.kind}`}>
          <Alert tone={notice.tone}>{noticeText(t, notice, cmp)}</Alert>
        </div>
      ))}
    </div>
  );
}

function KpiRow({
  cmp,
  shared,
  counts,
  basisLabel,
}: {
  cmp: RunCompareData;
  shared: string[];
  /** null when the cases are not compared (not aligned, or a side's results unreadable) */
  counts: CaseCounts | null;
  basisLabel: string;
}) {
  const { t } = useTranslation();
  useEvaluatorNames(); // N17-obs429: custom evaluators by name
  const { a, b } = cmp.runs;
  const composite = runComposite(a, b);
  // EVAL-TRUST 3.2: the backend's label when it sent one, else the same rule from the scores
  const sigOf = (id: string): Significance =>
    cmp.significance?.[id]?.label ?? significance(scoreEntry(a, id), scoreEntry(b, id));
  // a count, not the list: custom evaluator ids are long and would dwarf the other tiles
  const over = composite.evaluators.length ? (
    <span title={joined(t, composite.evaluators)} data-testid="v2-compare-kpi-composite-over">
      {t("v2.compare.kpi.sharedCount", { count: composite.evaluators.length })}
    </span>
  ) : (
    t("v2.compare.kpi.noShared")
  );
  return (
    <div className="v2-kpis v2-compare-kpis" data-testid="v2-compare-kpis">
      <Kpi
        label={t("v2.compare.kpi.composite")}
        value={<ScorePair a={composite.a} b={composite.b} chip sig={combined(composite.evaluators.map(sigOf))} />}
        sub={over}
        testId="v2-compare-kpi-composite"
      />
      {shared.map((id) => {
        const label = evaluatorLabel(t, id);
        return (
          <Kpi
            key={id}
            label={label}
            value={<ScorePair a={runScore(a, id)} b={runScore(b, id)} chip sig={sigOf(id)} />}
            sub={label !== id ? <span className="mono">{id}</span> : undefined}
            testId={`v2-compare-kpi-${id}`}
          />
        );
      })}
      {counts && counts.shared > 0 && (
        <Kpi
          label={t("v2.compare.kpi.cases")}
          value={t("v2.compare.kpi.casesValue", { down: counts.down, up: counts.up })}
          sub={<CaseStack counts={counts} basisLabel={basisLabel} />}
          testId="v2-compare-kpi-cases"
        />
      )}
    </div>
  );
}

const STACK: ("down" | "same" | "up" | "unscored")[] = ["down", "same", "up", "unscored"];

function CaseStack({ counts, basisLabel }: { counts: CaseCounts; basisLabel: string }) {
  const { t } = useTranslation();
  const parts = STACK.filter((k) => counts[k] > 0);
  return (
    <>
      <div className="v2-compare-stack" aria-hidden="true">
        {parts.map((k) => (
          <span key={k} className={k} style={{ width: `${(counts[k] / counts.shared) * 100}%` }} />
        ))}
      </div>
      <div className="v2-compare-legend">
        {parts.map((k) => (
          <span key={k}>
            <i className={k} />
            {t(`v2.compare.cls.${k}`)} {counts[k]}
          </span>
        ))}
      </div>
      <div>{t("v2.compare.kpi.basis", { basis: basisLabel })}</div>
    </>
  );
}

function ConfigCard({ cmp }: { cmp: RunCompareData }) {
  const { t } = useTranslation();
  const { a, b } = cmp.runs;
  const config = cmp.config;
  let sub: string | undefined;
  let body: ReactNode;
  if (config == null) {
    const why = agentKeyOf(a) !== agentKeyOf(b) ? "otherAgent" : !a.snapshot || !b.snapshot ? "unknown" : "hidden";
    body = <p className="v2-compare-note">{t(`v2.compare.config.${why}`)}</p>;
  } else if (config.same) {
    body = <p className="v2-compare-note">{t("v2.compare.config.same", { seq: config.seq })}</p>;
  } else {
    sub = t("v2.compare.config.range", { from: config.from_seq, to: config.to_seq });
    body = (
      <>
        {config.from_seq > config.to_seq && (
          <Alert tone="info">{t("v2.compare.config.backwards", { from: config.from_seq, to: config.to_seq })}</Alert>
        )}
        <h3 className="v2-sub-title">{t("v2.compare.config.publishes")}</h3>
        <ul className="v2-compare-between" data-testid="v2-compare-between">
          {config.between.map((snap) => {
            const label = noteLabel(snap.note);
            return (
              <li key={snap.seq}>
                <span className="mono">#{snap.seq}</span>
                <span>{fmtTime(snap.created_at)}</span>
                <span>{snap.created_by_name || "—"}</span>
                {label && <span>{"text" in label ? label.text : t(label.key, label.params)}</span>}
                {!snap.aws_version && <Tag tone="gray">{t("v2.agents.snapshots.state.not_deployed")}</Tag>}
              </li>
            );
          })}
        </ul>
        <h3 className="v2-sub-title">{t("v2.compare.config.changes")}</h3>
        <ConfigDiff agentId={config.agent_id} from={config.from_seq} to={config.to_seq} />
      </>
    );
  }
  return (
    <Card title={t("v2.compare.config.title")} sub={sub} testId="v2-compare-config">
      {body}
    </Card>
  );
}

function ConfigDiff({ agentId, from, to }: { agentId: string; from: number; to: number }) {
  const { t } = useTranslation();
  const diff = useLoad<SnapshotDiff>(() => api.snapshotDiff(agentId, from, to), `compare-diff:${agentId}:${from}:${to}`);
  if (diff.loading && !diff.data) return <Spin />;
  if (diff.error && !diff.data) return <Alert tone="error">{diff.error}</Alert>;
  if (!diff.data) return null;
  if (diff.data.changes.length === 0) return <Alert tone="info">{t("v2.agents.snapshots.noChanges")}</Alert>;
  return <DiffBody agentId={agentId} currentSpec={NO_CURRENT_SPEC} from={from} to={to} changes={diff.data.changes} />;
}

function CasesCard({
  cmp,
  shared,
  basis,
  onBasis,
  assessed,
  counts,
}: {
  cmp: RunCompareData;
  shared: string[];
  basis: string;
  onBasis: (basis: string) => void;
  assessed: AssessedCase[];
  counts: CaseCounts;
}) {
  const { t } = useTranslation();
  useEvaluatorNames(); // N17-obs429: custom evaluators by name
  const [filter, setFilter] = useState<CaseFilter>("all");
  const [open, setOpen] = useState<Set<string>>(() => new Set());
  const rows = useMemo(() => sortCases(filterCases(assessed, filter)), [assessed, filter]);
  const paged = usePaged(rows, CASE_PAGE);
  const columns = caseColumns(shared, basis);
  const results = cmp.results;
  const unreadable = SIDES.filter((s) => results && !results[s].available);
  const truncated = SIDES.filter((s) => results?.[s].truncated);
  const sub = t(cmp.alignment === "scenario" ? "v2.compare.cases.subScenario" : "v2.compare.cases.subOrder");

  if (!results || unreadable.length) {
    return (
      <Card title={t("v2.compare.cases.title")} sub={sub} testId="v2-compare-cases">
        <div data-testid="v2-compare-cases-unreadable">
          {SIDES.filter((s) => !results || !results[s].available).map((s) => (
            <Alert key={s} tone="warn">
              {t("v2.compare.sideNote", {
                side: SIDE_LABEL[s],
                note: t(`v2.taskDetail.reason.${results?.[s].reason ?? "unreadable"}`),
              })}
            </Alert>
          ))}
        </div>
        <p className="v2-compare-note">{t("v2.compare.cases.unreadableHint")}</p>
      </Card>
    );
  }

  const toggle = (id: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const pickFilter = (value: CaseFilter) => {
    setFilter(value);
    paged.setPage(1);
  };
  const span = columns.length + 3;

  return (
    <Card title={t("v2.compare.cases.title")} sub={sub} testId="v2-compare-cases">
      {truncated.map((s) => (
        <Alert key={s} tone="info">
          {t("v2.compare.sideNote", { side: SIDE_LABEL[s], note: t("v2.taskDetail.truncated") })}
        </Alert>
      ))}
      <div className="v2-toolbar">
        <div data-testid="v2-compare-filter">
          <Segmented<CaseFilter>
            value={filter}
            onChange={pickFilter}
            ariaLabel={t("v2.compare.cases.filterLabel")}
            options={(["all", "down", "up", "same"] as const).map((value) => ({
              value,
              label: t(`v2.compare.filter.${value}`, { count: counts[value] }),
            }))}
          />
        </div>
        <FilterSelect
          label={t("v2.compare.basis")}
          value={basis}
          onChange={(value) => {
            onBasis(value);
            paged.setPage(1);
          }}
          options={[
            { value: COMPOSITE_BASIS, label: t("v2.compare.composite") },
            ...shared.map((id) => ({ value: id, label: evaluatorLabel(t, id) })),
          ]}
          testId="v2-compare-basis"
        />
      </div>
      <div className="v2-table-wrap">
        <table className="v2-table v2-compare-table" data-testid="v2-compare-cases-table">
          <thead>
            <tr>
              <th style={{ width: 40 }}>
                <span className="sr-only">{t("v2.common.expand")}</span>
              </th>
              <th>{t("v2.compare.cases.colCase")}</th>
              {columns.map((id) => (
                <th key={id} className="ev">
                  <EvaluatorName id={id} />
                  <span className="sub">A → B</span>
                </th>
              ))}
              <th>{t("v2.compare.cases.colChange")}</th>
            </tr>
          </thead>
          <tbody>
            {paged.slice.length === 0 ? (
              <tr>
                <td colSpan={span}>
                  <div className="v2-table-empty">{t("v2.compare.cases.empty")}</div>
                </td>
              </tr>
            ) : (
              paged.slice.map((item) => (
                <CaseRow
                  key={item.case.scenario_id}
                  item={item}
                  columns={columns}
                  shared={shared}
                  open={open.has(item.case.scenario_id)}
                  onToggle={() => toggle(item.case.scenario_id)}
                />
              ))
            )}
          </tbody>
        </table>
      </div>
      {paged.pages > 1 && <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />}
    </Card>
  );
}

function CaseRow({
  item,
  columns,
  shared,
  open,
  onToggle,
}: {
  item: AssessedCase;
  columns: string[];
  shared: string[];
  open: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const c = item.case;
  const moved = item.cls === "up" || item.cls === "down";
  return (
    <Fragment>
      <tr data-testid={`v2-compare-case-${c.scenario_id}`} data-class={item.cls}>
        <td>
          <button
            type="button"
            className="v2-compare-chev"
            aria-expanded={open}
            aria-label={t(open ? "v2.common.collapse" : "v2.common.expand")}
            onClick={onToggle}
            data-testid={`v2-compare-expand-${c.scenario_id}`}
          >
            {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
          </button>
        </td>
        <td className="v2-compare-case">
          <span className="id">{c.scenario_id}</span>
          <span className="q" title={c.input}>
            {c.input || "—"}
          </span>
        </td>
        {columns.map((id) => {
          const cls = classify(c.a, c.b, [id]);
          return (
            <td key={id} className="ev nowrap">
              <ScorePair a={sideScore(c.a, id)} b={sideScore(c.b, id)} tone={cls === "up" || cls === "down" ? cls : undefined} />
            </td>
          );
        })}
        <td className="nowrap">
          <span className="v2-compare-change">
            <Tag tone={CLASS_TONE[item.cls]}>{t(`v2.compare.cls.${item.cls}`)}</Tag>
            {moved && <DeltaChip value={item.delta} />}
          </span>
        </td>
      </tr>
      {open && (
        <tr className="v2-compare-expand" data-testid={`v2-compare-judges-${c.scenario_id}`}>
          <td colSpan={columns.length + 3}>
            <div className="v2-compare-judges">
              <Judges side="a" data={c.a} shared={shared} />
              <Judges side="b" data={c.b} shared={shared} />
            </div>
          </td>
        </tr>
      )}
    </Fragment>
  );
}

/** One side's judge records of a case: the shared evaluators first, then the rest
 *  (marked as not compared); every record of a span-level evaluator is listed. */
function Judges({ side, data, shared }: { side: Side; data: RunCompareSide | null; shared: string[] }) {
  const { t } = useTranslation();
  useEvaluatorNames(); // N17-obs429: custom evaluators by name
  const present = data ? [...new Set(data.results.map((r) => r.evaluator_id))] : [];
  const order = [...shared.filter((id) => present.includes(id)), ...present.filter((id) => !shared.includes(id))];
  return (
    <div className="v2-compare-judge-col">
      <div className="hd">
        <span className="v2-compare-ab sm">{SIDE_LABEL[side]}</span>
        {t("v2.compare.case.judges")}
        {data && <span className="sid">{data.session_id}</span>}
      </div>
      {!data ? (
        <span className="v2-muted">{t("v2.compare.case.noSession")}</span>
      ) : order.length === 0 ? (
        <span className="v2-muted">{t("v2.compare.case.noResults")}</span>
      ) : (
        order.flatMap((id) =>
          data.results
            .filter((r) => r.evaluator_id === id)
            .map((r, i) => {
              const text = r.error_message || r.error_type || r.explanation;
              return (
                <div key={`${id}:${i}`} className="v2-compare-judge">
                  <div className="l">
                    <EvaluatorName id={id} />
                    {r.score == null ? (
                      <Tag tone="orange">{t("v2.outcome.error")}</Tag>
                    ) : (
                      <Score value={normalizedScore(r.score, id)} evaluatorId={id} />
                    )}
                    {r.label && <span className="v2-muted"><ScaleLabel evaluatorId={id} label={r.label} /></span>}
                    {!shared.includes(id) && <Tag tone="gray">{t("v2.compare.case.notCompared")}</Tag>}
                  </div>
                  {text && <p>{text}</p>}
                </div>
              );
            }),
        )
      )}
    </div>
  );
}

import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { methodLabel } from "../../../components/methodChipMeta";
import { api, type ScorecardRun } from "../../../lib/api";
import { evaluatorLabel } from "../../../lib/evaluators";
import { fmtScore } from "../../format";
import { useLoad, usePaged } from "../../hooks";
import { Score } from "../../ResultsView";
import {
  type AgentRow,
  agentMethods,
  agentRows,
  type AgentStatusFilter,
  type ChangeKind,
  filterAgentRows,
  rerunSource,
  shortDate,
  shortTime,
} from "../../scorecard";
import {
  Alert,
  type AttentionItem,
  AttentionPanel,
  Button,
  Card,
  type Column,
  FilterSelect,
  Lamp,
  LinkButton,
  Pager,
  SearchInput,
  Table,
  Tag,
} from "../../ui";
import { backState } from "./back";
import { RerunDialog } from "./RerunDialog";
import { SCORECARD_ATTENTION_CAP, scorecardAttention, type ScorecardEntry } from "./scorecardAttention";
import { benchName, rerunBlockText, runName, versionHint, versionShort } from "./scorecardText";
import { DeltaChip, Sparkline } from "./scorecardUi";

const PAGE_SIZE = 12;

const when = (value: string | null | undefined) => `${shortDate(value)} ${shortTime(value)}`.trim();

/**
 * 按 Agent (the 评估任务 default tab): one row per evaluated agent — its main benchmark,
 * latest composite, the change against the previous version (or why there is none), a
 * sparkline, counts, and 再评一次 — under a "needs a look" panel of regressions and
 * failed runs. Everything comes from one ledger-only scorecard read.
 */
export function AgentScoreList() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const location = useLocation();
  const { can } = useAuth();
  const sc = useLoad(() => api.evaluationScorecard(), "scorecard-list");
  const [q, setQ] = useState("");
  // deleted agents keep their runs on the ledger; they stay out of the list unless asked for
  const [status, setStatus] = useState<AgentStatusFilter>("active");
  const [method, setMethod] = useState("");
  const [change, setChange] = useState<"" | ChangeKind>("");
  const [rerun, setRerun] = useState<{ row: AgentRow; run: ScorecardRun } | null>(null);
  const mayRun = can("eval.run");

  const rows = useMemo(() => (sc.data ? agentRows(sc.data) : []), [sc.data]);
  const { filtered, hiddenDeleted } = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const matches = (list: AgentRow[]) =>
      needle
        ? list.filter((row) =>
            `${row.agent.name} ${row.agent.key} ${row.main ? benchName(t, row.main) : ""}`.toLowerCase().includes(needle),
          )
        : list;
    return {
      filtered: matches(filterAgentRows(rows, { status, method, change })),
      // what 已隐藏 N would add back: deleted rows that pass every other filter
      hiddenDeleted: status === "active" ? matches(filterAgentRows(rows, { status: "deleted", method, change })).length : 0,
    };
  }, [rows, q, t, status, method, change]);
  const methods = useMemo(() => agentMethods(rows), [rows]);
  const paged = usePaged(filtered, PAGE_SIZE);
  const attention = useMemo(() => (sc.data ? scorecardAttention(sc.data) : []), [sc.data]);

  const openAgent = (key: string, bench?: string) => setParams(bench ? { view: "agent", key, bench } : { view: "agent", key });
  const openCompare = (a: string, b: string) => setParams({ view: "compare", a, b });
  const openRun = (id: string) => setParams({ view: "detail", kind: "run", id }, { state: backState(location.search) });

  const attentionItem = (entry: ScorecardEntry): AttentionItem => {
    if (entry.kind === "failed") {
      return {
        key: entry.key,
        signal: entry.signal,
        title: t("v2.scorecard.attention.failed", { agent: entry.agent?.name ?? entry.run.agent_key, name: runName(entry.run) }),
        hint: entry.detail || t("v2.taskStatus.failed"),
        meta: when(entry.run.created_at),
        onOpen: () => openRun(entry.run.id),
        openLabel: t("v2.common.view"),
        testId: `v2-scorecard-att-failed-${entry.run.id}`,
      };
    }
    const lead = entry.worst[0];
    const title = entry.compositeDrop
      ? t("v2.scorecard.attention.regressionComposite", {
          agent: entry.agent.name,
          to: entry.to,
          from: entry.from,
          by: Math.abs(entry.composite.delta ?? 0).toFixed(2),
        })
      : t("v2.scorecard.attention.regressionEvaluator", {
          agent: entry.agent.name,
          to: entry.to,
          from: entry.from,
          evaluator: lead ? evaluatorLabel(t, lead.evaluatorId) : "",
          by: Math.abs(lead?.delta ?? 0).toFixed(2),
        });
    const changes = entry.worst
      .map((c) => t("v2.scorecard.attention.change", { name: evaluatorLabel(t, c.evaluatorId), a: fmtScore(c.a), b: fmtScore(c.b) }))
      .join(t("v2.scorecard.listSep"));
    const hint = [entry.kind === "pending" ? t("v2.scorecard.attention.pending") : null, benchName(t, entry.bench), changes]
      .filter(Boolean)
      .join(" · ");
    return {
      key: entry.key,
      signal: entry.signal,
      title,
      hint,
      meta: when(entry.latest.created_at),
      onOpen: () => openCompare(entry.previous.id, entry.latest.id),
      openLabel: t("v2.scorecard.attention.openCompare"),
      testId: `v2-scorecard-att-${entry.kind}-${entry.latest.id}`,
    };
  };

  const columns: Column<AgentRow>[] = [
    {
      key: "agent",
      title: t("v2.scorecard.col.agent"),
      className: "v2-sc-agent-col",
      render: (row) => {
        const { agent } = row;
        const meta = agent.log_source
          ? t("v2.tasks.cw.short")
          : [agent.method ? methodLabel(agent.method) : null, agent.current_seq != null ? t("v2.scorecard.current", { seq: agent.current_seq }) : null]
              .filter(Boolean)
              .join(" · ");
        return (
          <>
            <span className="v2-row v2-sc-agent">
              <LinkButton onClick={() => openAgent(agent.key)} testId={`v2-scorecard-open-${agent.key}`}>
                <span className="v2-fluid">
                  <span className={agent.log_source ? "ellipsis mono" : "ellipsis"} style={{ maxWidth: 200 }} title={agent.name}>
                    {agent.name}
                  </span>
                </span>
              </LinkButton>
              {agent.deleted && <Tag tone="gray">{t("v2.scorecard.deleted")}</Tag>}
            </span>
            {meta && <span className="sub">{meta}</span>}
          </>
        );
      },
    },
    {
      key: "main",
      // how the main benchmark is chosen, on hover (the toolbar holds the filters)
      title: <span title={t("v2.scorecard.listHint")}>{t("v2.scorecard.col.main")}</span>,
      render: (row) => {
        if (!row.main) return <span className="v2-muted">—</span>;
        const size = row.main.runs[row.main.runs.length - 1]?.benchmark.size;
        const name = benchName(t, row.main);
        return (
          <>
            <span className="v2-fluid">
              <span className="ellipsis" style={{ maxWidth: 180 }} title={name}>
                {name}
              </span>
            </span>
            <span className="sub">
              {/* the live kind lists three scopes and may wrap; the others keep one chunk */}
              <span className={row.main.kind === "live" ? undefined : "nowrap"}>{t(`v2.scorecard.benchKind.${row.main.kind}`)}</span>
              {size != null && row.main.kind !== "live" && (
                <>
                  {" "}
                  <span className="nowrap">· {t("v2.scorecard.benchSize", { count: size })}</span>
                </>
              )}
            </span>
          </>
        );
      },
    },
    {
      key: "latest",
      title: t("v2.scorecard.col.latest"),
      className: "nowrap",
      render: (row) => (
        <>
          <span className="v2-sc-big">
            <Score value={row.head.score} />
          </span>
          {row.head.latest && (
            <span className="sub" title={versionHint(t, row.head.latest.snapshot)}>
              {[versionShort(t, row.head.latest.snapshot), shortDate(row.head.latest.created_at)].filter(Boolean).join(" · ")}
            </span>
          )}
        </>
      ),
    },
    {
      key: "delta",
      title: t("v2.scorecard.col.vsPrev"),
      render: (row) => {
        const { head } = row;
        if (head.delta == null || !head.previous) {
          // a short label keeps the row at two lines; the full sentence is the tooltip
          const reason = head.reason ?? "noCompleted";
          return (
            <span className="v2-sc-reason" title={t(`v2.scorecard.reason.${reason}`)} data-testid={`v2-scorecard-reason-${row.agent.key}`}>
              {t(`v2.scorecard.reasonShort.${reason}`)}
            </span>
          );
        }
        return (
          <>
            <span className="v2-row nowrap" data-testid={`v2-scorecard-delta-${row.agent.key}`}>
              {head.signal && <Lamp signal={head.signal} />}
              <DeltaChip value={head.delta} />
            </span>
            <span className="sub">
              {head.byVersion && head.previous.snapshot
                ? t("v2.scorecard.vs.version", { seq: head.previous.snapshot.seq })
                : t("v2.scorecard.vs.run")}
            </span>
          </>
        );
      },
    },
    {
      key: "trend",
      title: t("v2.scorecard.col.trend"),
      render: (row) => (
        <Sparkline
          values={row.spark}
          label={t("v2.scorecard.sparkLabel", { values: row.spark.map((v) => fmtScore(v)).join(t("v2.compare.sep")) })}
          testId={`v2-scorecard-spark-${row.agent.key}`}
        />
      ),
    },
    {
      key: "evals",
      title: t("v2.scorecard.col.evals"),
      render: (row) => (
        <>
          <span className="nowrap">{t("v2.scorecard.counts.runs", { count: row.runs.length })}</span>
          {/* the two counts wrap onto two lines rather than widen the column */}
          <span className="sub">
            {row.versions ? (
              <span className="nowrap">{t("v2.scorecard.counts.versions", { count: row.versions })} ·</span>
            ) : (
              `${t("v2.scorecard.noVersion")} ·`
            )}{" "}
            <span className="nowrap">{t("v2.scorecard.counts.benches", { count: row.benches.length })}</span>
          </span>
        </>
      ),
    },
    {
      key: "last",
      title: t("v2.scorecard.col.lastEval"),
      className: "nowrap",
      render: (row) => (
        <>
          {when(row.newest?.created_at)}
          <span className="sub" title={t("v2.common.creator")}>
            {row.newest?.created_by_name || "—"}
          </span>
        </>
      ),
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right sticky",
      render: (row) => {
        const target = rerunSource(row.main, row.agent);
        const blocked = rerunBlockText(t, target.block, mayRun);
        return (
          <div className="v2-actions">
            <LinkButton onClick={() => openAgent(row.agent.key)}>{t("v2.common.view")}</LinkButton>
            <LinkButton
              disabled={!!blocked || !target.run}
              title={blocked}
              onClick={() => target.run && setRerun({ row, run: target.run })}
              testId={`v2-scorecard-rerun-${row.agent.key}`}
            >
              {t("v2.scorecard.rerun.action")}
            </LinkButton>
          </div>
        );
      },
    },
  ];

  return (
    <div className="v2-scorecard">
      {sc.data && (
        <AttentionPanel
          title={
            <>
              {t("v2.scorecard.attention.title")}
              <span className="sub">{t("v2.scorecard.attention.sub")}</span>
            </>
          }
          items={attention.map(attentionItem)}
          cap={SCORECARD_ATTENTION_CAP}
          dismissScope="eval.agents"
          testId="v2-scorecard-attention"
          empty={
            <>
              <Lamp signal={rows.length ? "ok" : "off"} />
              {rows.length ? t("v2.scorecard.attention.empty") : t("v2.scorecard.attention.emptyNone")}
            </>
          }
        />
      )}
      {sc.data?.truncated && <Alert tone="info">{t("v2.scorecard.truncated")}</Alert>}
      <Card>
        <div className="v2-toolbar">
          <Button onClick={sc.reload}>{t("v2.common.refresh")}</Button>
          <Button
            kind="primary"
            disabled={!mayRun}
            title={mayRun ? undefined : t("v2.tasks.noPermission")}
            onClick={() => setParams({ view: "new" }, { state: backState(location.search) })}
            testId="v2-scorecard-new"
          >
            <Plus size={14} aria-hidden="true" />
            {t("v2.tasks.new")}
          </Button>
          <FilterSelect
            label={t("v2.scorecard.filter.status")}
            value={status}
            allLabel={t("v2.common.all")}
            onChange={(v) => setStatus(v as AgentStatusFilter)}
            options={[
              { value: "active", label: t("v2.scorecard.filter.statusActive") },
              { value: "deleted", label: t("v2.scorecard.deleted") },
            ]}
            testId="v2-scorecard-filter-status"
          />
          <FilterSelect
            label={t("v2.scorecard.filter.method")}
            value={method}
            allLabel={t("v2.common.all")}
            onChange={setMethod}
            options={methods.map((m) => ({ value: m, label: m === "logs" ? t("v2.tasks.cw.short") : methodLabel(m) }))}
            testId="v2-scorecard-filter-method"
          />
          <FilterSelect
            label={t("v2.scorecard.col.vsPrev")}
            value={change}
            allLabel={t("v2.common.all")}
            onChange={(v) => setChange(v as "" | ChangeKind)}
            options={(["down", "up", "minor", "none"] as const).map((k) => ({ value: k, label: t(`v2.scorecard.filter.change.${k}`) }))}
            testId="v2-scorecard-filter-change"
          />
          {hiddenDeleted > 0 && (
            <LinkButton onClick={() => setStatus("")} testId="v2-scorecard-show-deleted">
              {t("v2.scorecard.filter.hiddenDeleted", { count: hiddenDeleted })}
            </LinkButton>
          )}
          <div className="end">
            <SearchInput value={q} onChange={setQ} placeholder={t("v2.scorecard.search")} testId="v2-scorecard-search" />
            <span className="v2-count">{t("v2.scorecard.agentCount", { count: filtered.length })}</span>
          </div>
        </div>
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(row) => row.agent.key}
          loading={sc.loading}
          error={sc.data ? null : sc.error}
          onRetry={sc.reload}
          empty={rows.length ? t("v2.scorecard.emptyFiltered") : t("v2.scorecard.empty")}
          testId="v2-scorecard-agents"
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
      {rerun && (
        <RerunDialog
          key={rerun.run.id}
          agent={rerun.row.agent}
          source={rerun.run}
          onClose={() => setRerun(null)}
          onStarted={() => {
            const { row, run } = rerun;
            setRerun(null);
            // the new run is the next point of that benchmark's line
            openAgent(row.agent.key, run.benchmark.key);
          }}
        />
      )}
    </div>
  );
}

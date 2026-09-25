import { useState } from "react";
import { useTranslation } from "react-i18next";

import type {
  SkillLabJobResults,
  SkillLabResultRow,
  SkillLabUsageCounter,
  SkillLabUsageRecord,
  SkillLabUsageSide,
} from "../../../lib/api";
import { Alert, Card, Drawer, Kpi, LinkButton, Segmented, Table, Tag, type TagTone } from "../../ui";

const pct = (value: number) => `${(value * 100).toFixed(1)}%`;
/** An unreported counter is unknown — rendered as a dash, never as 0. */
const count = (value: number | null) => (typeof value === "number" ? value.toLocaleString() : "—");
const seconds = (value: number | null) => (typeof value === "number" ? `${value.toFixed(1)}s` : "—");

const USAGE_COLUMNS: [SkillLabUsageCounter, string][] = [
  ["input", "input"],
  ["cache_write", "cacheWrite"],
  ["cache_read", "cacheRead"],
  ["output", "output"],
  ["unattributed", "unattributed"],
];
const USAGE_SIDES = ["target", "judge"] as const;

/** Report coverage and breakdown completeness are distinct — neither may read as complete when it is not. */
function coverageState(side: SkillLabUsageSide): { tone: TagTone; key: string } {
  if (side.complete) return { tone: "green", key: "complete" };
  if (side.reports_complete) return { tone: "orange", key: "breakdownPartial" };
  if (side.reported_rows > 0 || side.malformed_rows > 0) return { tone: "orange", key: "partial" };
  return { tone: "gray", key: "none" };
}

/** `k/n` marker for a summed counter only some rows reported (a partial sum). */
function partialMark(side: SkillLabUsageSide | SkillLabUsageRecord, key: SkillLabUsageCounter) {
  if (!("rows" in side) || key === "unattributed" || side[key] === null) return null;
  const rows = side.counter_rows[key];
  return rows < side.rows ? `${rows}/${side.rows}` : null;
}

const RECORD_TONE: Record<SkillLabUsageRecord["status"], TagTone> = {
  reported: "green",
  missing: "gray",
  malformed: "orange",
};

type UsageRow = { name: (typeof USAGE_SIDES)[number]; side: SkillLabUsageSide | SkillLabUsageRecord };

/** Token counters per side: the run summary (with coverage) or one task's records (with status). */
export function UsageTable({
  sides,
  testId,
}: {
  sides: Record<(typeof USAGE_SIDES)[number], SkillLabUsageSide | SkillLabUsageRecord>;
  testId: string;
}) {
  const { t } = useTranslation();
  return (
    <Table
      density="dense"
      testId={testId}
      rows={USAGE_SIDES.map((name) => ({ name, side: sides[name] }))}
      rowKey={(r: UsageRow) => r.name}
      columns={[
        { key: "side", title: t("skillLab.eval.usage.col.side"), render: (r: UsageRow) => t(`skillLab.eval.usage.side.${r.name}`) },
        ...USAGE_COLUMNS.map(([key, label]) => ({
          key,
          title: (
            <span title={key === "unattributed" ? t("skillLab.eval.usage.unattributedHint") : undefined}>
              {t(`skillLab.eval.usage.col.${label}`)}
            </span>
          ),
          className: "num",
          render: (r: UsageRow) => {
            const partial = partialMark(r.side, key);
            const summary = "rows" in r.side ? r.side : null;
            return (
              <span className={r.side[key] === null ? "v2-muted" : undefined}>
                {count(r.side[key])}
                {partial && (
                  <span
                    className="v2-muted"
                    style={{ marginLeft: 4, fontSize: 12 }}
                    title={t("skillLab.eval.usage.partialCounterHint", { rows: summary?.counter_rows[key], total: summary?.rows })}
                  >
                    {partial}
                  </span>
                )}
              </span>
            );
          },
        })),
        {
          key: "coverage",
          title: t("skillLab.eval.usage.col.coverage"),
          render: (r: UsageRow) => {
            if ("rows" in r.side) {
              const summary = r.side;
              const state = coverageState(summary);
              return (
                <span className="v2-row" style={{ flexWrap: "nowrap" }}>
                  <Tag tone={state.tone}>{t(`skillLab.eval.usage.state.${state.key}`)}</Tag>
                  <span className="v2-muted nowrap">
                    {t("skillLab.eval.usage.coverage", { reported: summary.reported_rows, rows: summary.rows })}
                    {summary.malformed_rows > 0 && ` · ${t("skillLab.eval.usage.malformed", { count: summary.malformed_rows })}`}
                  </span>
                </span>
              );
            }
            const record = r.side;
            return <Tag tone={RECORD_TONE[record.status]}>{t(`skillLab.eval.usage.state.${record.status}`)}</Tag>;
          },
        },
      ]}
    />
  );
}

/** Verdict: an invalid row is an infrastructure failure, never a zero. */
function verdict(row: SkillLabResultRow): { tone: TagTone; key: string } {
  if (row.score_valid === false) return { tone: "orange", key: "invalid" };
  if (row.hard) return { tone: "green", key: "pass" };
  return { tone: "red", key: "fail" };
}

const detailRows = (row: SkillLabResultRow): [string, string][] =>
  (
    [
      ["judgeStatus", row.judge_status],
      ["judgeReason", row.judge_reason],
      ["judgeError", row.judge_error],
      ["error", row.error],
      ["response", row.response],
    ] as [string, string | null][]
  ).filter((entry): entry is [string, string] => Boolean(entry[1]));

/** Judged results of an eval job: KPIs, token usage, notes and the per-task table. */
export function EvalResults({ results }: { results: SkillLabJobResults }) {
  const { t } = useTranslation();
  const [openId, setOpenId] = useState<string | null>(null);
  const [verdictFilter, setVerdictFilter] = useState<string>("");
  const { summary, rows } = results;
  const opened = rows.find((row) => row.id === openId) ?? null;
  const shown = verdictFilter ? rows.filter((row) => verdict(row).key === verdictFilter) : rows;

  return (
    <>
      <div className="v2-kpis" data-testid="v2-eval-kpis">
        <Kpi
          label={t("skillLab.eval.stat.passRate")}
          value={pct(summary.pass_rate)}
          sub={t("skillLab.eval.stat.passedOf", { passed: summary.passed, scored: summary.tasks - summary.invalid })}
          tone={summary.pass_rate >= 0.7 ? "good" : summary.pass_rate < 0.4 ? "bad" : undefined}
        />
        <Kpi label={t("skillLab.eval.stat.softMean")} value={summary.soft_mean.toFixed(3)} sub={t("skillLab.eval.stat.softFoot")} />
        <Kpi
          label={t("skillLab.eval.stat.invalid")}
          value={summary.invalid}
          sub={t("skillLab.eval.stat.invalidFoot")}
          tone={summary.invalid > 0 ? "bad" : undefined}
        />
        <Kpi
          label={t("skillLab.eval.stat.duration")}
          value={`${summary.duration_s.toFixed(0)}s`}
          sub={t("skillLab.eval.stat.durationFoot", { n: summary.tasks })}
        />
      </div>
      {summary.invalid > 0 && <Alert tone="warn">{t("skillLab.eval.invalidNote", { count: summary.invalid })}</Alert>}
      {/* A missing host judge CLI is the operator's to fix — name it rather than leave a stack trace. */}
      {(summary.judge_prerequisite_missing?.length ?? 0) > 0 && (
        <Alert tone="error">
          {t("skillLab.eval.judgePrerequisiteNote", { clis: summary.judge_prerequisite_missing.join(", ") })}
        </Alert>
      )}
      <Card title={t("skillLab.eval.usage.title")} testId="v2-eval-usage">
        <UsageTable sides={summary.token_usage} testId="v2-eval-usage-table" />
        <p className="v2-muted v2-skilllab-hint">{t("skillLab.eval.usage.scope")}</p>
      </Card>
      <Card
        title={t("v2.skillLab.taskResults")}
        end={
          <Segmented
            value={verdictFilter}
            onChange={setVerdictFilter}
            options={(["", "pass", "fail", "invalid"] as const).map((key) => ({
              value: key,
              label: key ? t(`skillLab.eval.verdict.${key}`) : t("v2.common.all"),
            }))}
          />
        }
        testId="v2-eval-results"
      >
        <Table
          columns={[
            {
              key: "task",
              title: t("skillLab.eval.col.task"),
              render: (row: SkillLabResultRow) => (
                <>
                  <LinkButton onClick={() => setOpenId(row.id)} testId={`v2-eval-result-${row.id}`}>
                    <span className="mono">{row.id}</span>
                  </LinkButton>
                  {row.task_type && <span className="sub mono">{row.task_type}</span>}
                </>
              ),
            },
            {
              key: "verdict",
              title: t("skillLab.eval.col.verdict"),
              render: (row: SkillLabResultRow) => {
                const v = verdict(row);
                return <Tag tone={v.tone}>{t(`skillLab.eval.verdict.${v.key}`)}</Tag>;
              },
            },
            {
              key: "soft",
              title: t("skillLab.eval.col.soft"),
              className: "num",
              render: (row: SkillLabResultRow) =>
                typeof row.soft === "number" ? (
                  <span className={`v2-score ${row.soft >= 0.7 ? "good" : row.soft >= 0.4 ? "mid" : "bad"}`}>{row.soft.toFixed(2)}</span>
                ) : (
                  "—"
                ),
            },
            {
              key: "reason",
              title: t("skillLab.eval.detail.judgeReason"),
              render: (row: SkillLabResultRow) => (
                <span className="ellipsis" style={{ maxWidth: 420 }} title={row.judge_reason ?? row.error ?? ""}>
                  {row.judge_reason ?? row.judge_error ?? row.error ?? "—"}
                </span>
              ),
            },
            { key: "dur", title: t("skillLab.eval.col.taskDuration"), className: "num", render: (row: SkillLabResultRow) => seconds(row.duration_s) },
            { key: "files", title: t("skillLab.eval.col.artifacts"), className: "num", render: (row: SkillLabResultRow) => row.artifacts.length || "—" },
            {
              key: "ops",
              title: t("v2.common.actions"),
              className: "right",
              render: (row: SkillLabResultRow) => <LinkButton onClick={() => setOpenId(row.id)}>{t("v2.common.detail")}</LinkButton>,
            },
          ]}
          rows={shown}
          rowKey={(row) => row.id}
          empty={t("skillLab.eval.noTasks")}
          testId="v2-eval-results-table"
        />
      </Card>
      <Drawer
        open={opened !== null}
        title={<span className="mono">{opened?.id}</span>}
        onClose={() => setOpenId(null)}
        testId="v2-eval-result-drawer"
      >
        {opened && (
          <div className="v2-stack">
            <div className="v2-row">
              <Tag tone={verdict(opened).tone}>{t(`skillLab.eval.verdict.${verdict(opened).key}`)}</Tag>
              {opened.task_type && <Tag tone="outline">{opened.task_type}</Tag>}
              <span className="v2-muted">
                {t("skillLab.eval.col.soft")} {typeof opened.soft === "number" ? opened.soft.toFixed(2) : "—"} · {seconds(opened.duration_s)}
              </span>
            </div>
            {detailRows(opened).length === 0 && <span className="v2-muted">{t("skillLab.eval.noDetail")}</span>}
            {detailRows(opened).map(([key, value]) => (
              <div key={key}>
                <h3 className="v2-sub-title">{t(`skillLab.eval.detail.${key}`)}</h3>
                <pre className="v2-pre">{value}</pre>
              </div>
            ))}
            <div>
              <h3 className="v2-sub-title">{t("skillLab.eval.detail.usage")}</h3>
              <UsageTable sides={opened.token_usage} testId={`v2-eval-usage-row-${opened.id}`} />
            </div>
            {opened.artifacts.length > 0 && (
              <div>
                <h3 className="v2-sub-title">{t("skillLab.eval.detail.artifacts")}</h3>
                <pre className="v2-pre">{opened.artifacts.map((a) => a.path ?? "?").join("\n")}</pre>
              </div>
            )}
          </div>
        )}
      </Drawer>
    </>
  );
}

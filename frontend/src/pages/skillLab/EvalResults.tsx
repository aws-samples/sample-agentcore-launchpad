import type { CSSProperties } from "react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { ChipTone } from "../../components";
import { Chip, StatTile } from "../../components";
import type {
  SkillLabJobResults,
  SkillLabResultRow,
  SkillLabUsageCounter,
  SkillLabUsageRecord,
  SkillLabUsageSide,
} from "../../lib/api";

const pct = (value: number) => `${(value * 100).toFixed(1)}%`;

/** An unreported counter is unknown — rendered as a dash, never as 0. */
const count = (value: number | null) => (typeof value === "number" ? value.toLocaleString() : "—");

const USAGE_COLUMNS: [SkillLabUsageCounter, string][] = [
  ["input", "input"],
  ["cache_write", "cacheWrite"],
  ["cache_read", "cacheRead"],
  ["output", "output"],
  ["unattributed", "unattributed"],
];

const USAGE_SIDES = ["target", "judge"] as const;

/** Coverage chip for one side of the run summary: only a run where every task
 *  reported cleanly may read as complete. */
function coverageState(side: SkillLabUsageSide): { tone: ChipTone; key: string } {
  if (side.complete) return { tone: "good", key: "complete" };
  if (side.reported_rows > 0 || side.malformed_rows > 0) return { tone: "warn", key: "partial" };
  return { tone: "muted", key: "none" };
}

const recordTone: Record<SkillLabUsageRecord["status"], ChipTone> = {
  reported: "good",
  missing: "muted",
  malformed: "warn",
};

const cellStyle = { fontSize: 10.5, padding: "4px 8px" } as const;

/**
 * Token counters per side. Renders either the run summary (`SkillLabUsageSide`
 * with a coverage cell) or one task's records (`SkillLabUsageRecord` with a
 * status chip); the column set is the same so the eye can line them up.
 */
function UsageTable({
  sides,
  testId,
}: {
  sides: Record<(typeof USAGE_SIDES)[number], SkillLabUsageSide | SkillLabUsageRecord>;
  testId: string;
}) {
  const { t } = useTranslation();
  return (
    <table data-testid={testId} style={{ marginTop: 3 }}>
      <thead>
        <tr>
          <th style={cellStyle}>{t("skillLab.eval.usage.col.side")}</th>
          {USAGE_COLUMNS.map(([key, label]) => (
            <th
              key={key}
              style={cellStyle}
              title={key === "unattributed" ? t("skillLab.eval.usage.unattributedHint") : undefined}
            >
              {t(`skillLab.eval.usage.col.${label}`)}
            </th>
          ))}
          <th style={cellStyle}>{t("skillLab.eval.usage.col.coverage")}</th>
        </tr>
      </thead>
      <tbody>
        {USAGE_SIDES.map((name) => {
          const side = sides[name];
          const summary = "rows" in side ? side : null;
          const state = summary ? coverageState(summary) : null;
          return (
            <tr key={name} data-testid={`${testId}-${name}`}>
              <td className="pri" style={cellStyle}>
                {t(`skillLab.eval.usage.side.${name}`)}
              </td>
              {USAGE_COLUMNS.map(([key]) => (
                <td
                  key={key}
                  className={side[key] === null ? "mono dim" : "mono"}
                  style={cellStyle}
                  data-counter={key}
                >
                  {count(side[key])}
                </td>
              ))}
              <td style={cellStyle}>
                {summary && state ? (
                  <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                    <Chip tone={state.tone}>{t(`skillLab.eval.usage.state.${state.key}`)}</Chip>
                    <span className="mono dim" style={{ fontSize: 10.5 }}>
                      {t("skillLab.eval.usage.coverage", {
                        reported: summary.reported_rows,
                        rows: summary.rows,
                      })}
                      {summary.malformed_rows > 0 &&
                        ` · ${t("skillLab.eval.usage.malformed", { count: summary.malformed_rows })}`}
                    </span>
                  </span>
                ) : (
                  <Chip tone={recordTone[(side as SkillLabUsageRecord).status]}>
                    {t(`skillLab.eval.usage.state.${(side as SkillLabUsageRecord).status}`)}
                  </Chip>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

const seconds = (value: number | null) =>
  typeof value === "number" ? `${value.toFixed(1)}s` : "—";

/** Verdict chip: an invalid row is an infrastructure failure, never a zero. */
function verdict(row: SkillLabResultRow) {
  if (row.score_valid === false) return { tone: "warn" as const, icon: "!", key: "invalid" };
  if (row.hard) return { tone: "good" as const, icon: "✓", key: "pass" };
  return { tone: "crit" as const, icon: "✕", key: "fail" };
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

export function EvalResults({ results }: { results: SkillLabJobResults }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState<Set<string>>(new Set());

  const toggle = (id: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const { summary, rows } = results;

  return (
    <div data-testid="skill-lab-eval-results">
      <div className="tiles" data-testid="eval-summary-tiles">
        <StatTile
          label={t("skillLab.eval.stat.passRate")}
          value={pct(summary.pass_rate)}
          foot={t("skillLab.eval.stat.passedOf", {
            passed: summary.passed,
            scored: summary.tasks - summary.invalid,
          })}
          style={{ "--i": 0 } as CSSProperties}
        />
        <StatTile
          label={t("skillLab.eval.stat.softMean")}
          value={summary.soft_mean.toFixed(3)}
          foot={t("skillLab.eval.stat.softFoot")}
          style={{ "--i": 1 } as CSSProperties}
        />
        <StatTile
          label={t("skillLab.eval.stat.invalid")}
          value={String(summary.invalid)}
          foot={t("skillLab.eval.stat.invalidFoot")}
          style={{ "--i": 2 } as CSSProperties}
        />
        <StatTile
          label={t("skillLab.eval.stat.duration")}
          value={summary.duration_s.toFixed(0)}
          unit="s"
          foot={t("skillLab.eval.stat.durationFoot", { n: summary.tasks })}
          style={{ "--i": 3 } as CSSProperties}
        />
      </div>

      {/* Token usage is what the transcripts reported, summed over every task
          (invalid rows included — the tokens were spent). Coverage states how
          much of the run that is; a dash is an unreported counter, not zero. */}
      <div style={{ marginBottom: 10 }} data-testid="eval-usage-summary">
        <div className="mono dim" style={{ fontSize: 10, letterSpacing: ".08em" }}>
          {t("skillLab.eval.usage.title")}
        </div>
        <UsageTable sides={summary.token_usage} testId="eval-usage-table" />
        <div className="dim" style={{ fontSize: 10.5, marginTop: 4 }}>
          {t("skillLab.eval.usage.scope")}
        </div>
      </div>

      {summary.invalid > 0 && (
        <div className="note" style={{ marginBottom: 10 }} data-testid="eval-invalid-note">
          <span className="i">[!]</span>
          <span>{t("skillLab.eval.invalidNote", { count: summary.invalid })}</span>
        </div>
      )}

      {/* A missing host judge CLI is the operator's to fix, so name it instead of
          leaving them to read a stack trace on an invalid row. */}
      {(summary.judge_prerequisite_missing?.length ?? 0) > 0 && (
        <div
          className="note"
          style={{ borderColor: "var(--crit)", marginBottom: 10 }}
          data-testid="eval-judge-prerequisite-note"
        >
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span>
            {t("skillLab.eval.judgePrerequisiteNote", {
              clis: summary.judge_prerequisite_missing.join(", "),
            })}
          </span>
        </div>
      )}

      <table data-testid="eval-results-table">
        <thead>
          <tr>
            <th>{t("skillLab.eval.col.task")}</th>
            <th>{t("skillLab.eval.col.verdict")}</th>
            <th>{t("skillLab.eval.col.soft")}</th>
            <th>{t("skillLab.eval.col.taskDuration")}</th>
            <th>{t("skillLab.eval.col.artifacts")}</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const chip = verdict(row);
            const isOpen = open.has(row.id);
            const details = detailRows(row);
            return [
              <tr
                key={row.id}
                data-testid={`eval-result-row-${row.id}`}
                style={{ cursor: "pointer" }}
                onClick={() => toggle(row.id)}
              >
                <td className="pri mono">
                  {isOpen ? "▾" : "▸"} {row.id}
                  {row.task_type && (
                    <span className="dim" style={{ marginLeft: 6, fontSize: 10.5 }}>
                      {row.task_type}
                    </span>
                  )}
                </td>
                <td>
                  <Chip tone={chip.tone} icon={chip.icon}>
                    {t(`skillLab.eval.verdict.${chip.key}`)}
                  </Chip>
                </td>
                <td className="mono">
                  {typeof row.soft === "number" ? row.soft.toFixed(2) : "—"}
                </td>
                <td className="mono dim">{seconds(row.duration_s)}</td>
                <td className="mono dim">{row.artifacts.length || "—"}</td>
              </tr>,
              isOpen && (
                <tr key={`${row.id}-detail`} data-testid={`eval-result-detail-${row.id}`}>
                  <td colSpan={5} style={{ background: "rgba(255,255,255,.015)" }}>
                    {details.length === 0 && (
                      <span className="dim mono" style={{ fontSize: 10.5 }}>
                        {t("skillLab.eval.noDetail")}
                      </span>
                    )}
                    {details.map(([key, value]) => (
                      <div key={key} style={{ marginBottom: 8 }}>
                        <div className="mono dim" style={{ fontSize: 10, letterSpacing: ".08em" }}>
                          {t(`skillLab.eval.detail.${key}`)}
                        </div>
                        <pre
                          className="code"
                          style={{
                            margin: "3px 0 0",
                            maxHeight: 200,
                            overflow: "auto",
                            whiteSpace: "pre-wrap",
                            overflowWrap: "anywhere",
                            fontSize: 10.5,
                          }}
                        >
                          {value}
                        </pre>
                      </div>
                    ))}
                    <div style={{ marginBottom: 8 }}>
                      <div className="mono dim" style={{ fontSize: 10, letterSpacing: ".08em" }}>
                        {t("skillLab.eval.detail.usage")}
                      </div>
                      <UsageTable sides={row.token_usage} testId={`eval-usage-row-${row.id}`} />
                    </div>
                    {row.artifacts.length > 0 && (
                      <div>
                        <div className="mono dim" style={{ fontSize: 10, letterSpacing: ".08em" }}>
                          {t("skillLab.eval.detail.artifacts")}
                        </div>
                        <div className="code" style={{ marginTop: 3, fontSize: 10.5 }}>
                          {row.artifacts.map((a) => a.path ?? "?").join("\n")}
                        </div>
                      </div>
                    )}
                  </td>
                </tr>
              ),
            ];
          })}
          {rows.length === 0 && (
            <tr>
              <td colSpan={5} className="dim mono" style={{ textAlign: "center" }}>
                {t("skillLab.eval.noTasks")}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

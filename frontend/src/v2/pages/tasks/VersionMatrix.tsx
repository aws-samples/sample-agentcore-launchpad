import { useTranslation } from "react-i18next";

import type { ScorecardRun, ScorecardVersion } from "../../../lib/api";
import { evaluatorLabel } from "../../../lib/evaluators";
import { fmtScore } from "../../format";
import { type MatrixCell, type MatrixRow, seriesColor, shortDate } from "../../scorecard";
import { type Column, LinkButton, Table, Tag } from "../../ui";
import { noteLabel } from "../agents/snapshots";
import { DeltaChip } from "./scorecardUi";

/** A one-hue tint carries the score (stronger = higher); the chip carries the direction. */
function tint(value: number | null): string | undefined {
  if (value == null) return undefined;
  const pct = Math.round(2 + Math.max(0, value - 0.1) * 14);
  return `color-mix(in srgb, var(--v2-primary) ${pct}%, var(--v2-card))`;
}

/** One tinted score with its change chip (also the architect's evaluator breakdown). */
export function MatrixScore({ cell, testId }: { cell: MatrixCell; testId?: string }) {
  return (
    <span className="v2-matrix-cell" style={{ background: tint(cell.value) }} data-testid={testId}>
      <span className="val">{fmtScore(cell.value)}</span>
      {cell.value != null && cell.delta != null && <DeltaChip value={cell.delta} />}
    </span>
  );
}

/**
 * Version × evaluator (dataset benchmarks): one row per agent version, newest first,
 * with the mean per evaluator and of the composite over that version's runs, each with
 * its change against the next older row, and a link that compares the two versions'
 * latest runs.
 */
export function VersionMatrix({
  rows,
  columns,
  hiddenCount,
  slots,
  versions,
  currentSeq,
  onCompare,
}: {
  rows: MatrixRow[];
  columns: string[];
  /** evaluators of the benchmark left out of the columns (still on the trend) */
  hiddenCount: number;
  slots: Map<string, number | null>;
  versions: ScorecardVersion[];
  currentSeq: number | null;
  onCompare: (older: ScorecardRun, newer: ScorecardRun) => void;
}) {
  const { t } = useTranslation();
  const bySeq = new Map(versions.map((v) => [v.seq, v]));
  const table: Column<MatrixRow>[] = [
    {
      key: "version",
      title: t("v2.scorecard.matrix.version"),
      render: (row) => {
        const v = bySeq.get(row.seq);
        const note = noteLabel(v?.note);
        const meta = [
          v ? shortDate(v.created_at) : null,
          v?.created_by_name || null,
          note ? ("text" in note ? note.text : t(note.key, note.params)) : null,
        ].filter(Boolean);
        return (
          <div className="v2-sc-ver">
            <b>
              {row.approx ? `≈ #${row.seq}` : `#${row.seq}`}
              {row.seq === currentSeq && <Tag tone="blue">{t("v2.scorecard.currentShort")}</Tag>}
            </b>
            {meta.length > 0 && (
              <span className="sub ellipsis" style={{ maxWidth: 220 }} title={meta.join(" · ")}>
                {meta.join(" · ")}
              </span>
            )}
          </div>
        );
      },
    },
    {
      key: "runs",
      title: t("v2.scorecard.matrix.runs"),
      className: "num",
      render: (row) => row.runs,
    },
    ...columns.map(
      (id, c): Column<MatrixRow> => ({
        key: `ev-${id}`,
        title: (
          <span className="v2-matrix-head" title={id}>
            <i style={{ background: seriesColor(slots.get(id)) }} aria-hidden="true" />
            <span className="ellipsis">{evaluatorLabel(t, id)}</span>
          </span>
        ),
        className: "nowrap",
        render: (row) => <MatrixScore cell={row.cells[c]} testId={`v2-scorecard-matrix-${row.seq}-${id}`} />,
      }),
    ),
    {
      key: "composite",
      title: (
        <span className="v2-matrix-head">
          <i className="thick" style={{ background: "var(--v2-series-composite)" }} aria-hidden="true" />
          {t("v2.scorecard.matrix.composite")}
        </span>
      ),
      className: "nowrap",
      render: (row) => <MatrixScore cell={row.composite} testId={`v2-scorecard-matrix-${row.seq}-composite`} />,
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (row) =>
        row.older ? (
          <LinkButton
            onClick={() => row.older && onCompare(row.older.latest, row.latest)}
            testId={`v2-scorecard-matrix-compare-${row.seq}`}
          >
            {t("v2.scorecard.matrix.compareWith", { seq: row.older.seq })}
          </LinkButton>
        ) : (
          <span className="v2-muted">—</span>
        ),
    },
  ];
  return (
    <>
      <Table columns={table} rows={rows} rowKey={(row) => String(row.seq)} testId="v2-scorecard-matrix" />
      {hiddenCount > 0 && <p className="v2-sc-caption">{t("v2.scorecard.matrix.more", { count: hiddenCount })}</p>}
    </>
  );
}

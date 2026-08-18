import { useTranslation } from "react-i18next";

import type { SkillLabTrainSummary } from "../../lib/api";

const W = 560;
const TOP = 10;
const BASE = 110;

/** Held-out gate scores are pass rates, so the axis is pinned to 0–1 rather
 *  than auto-scaled: a flat run should read as flat, not as full-height noise. */
const y = (value: number) => BASE - value * (BASE - TOP);

/**
 * Contiguous runs of scored steps. A step that never reached the gate (skipped
 * for want of patches) carries a null score and must break the line instead of
 * being drawn at zero.
 */
const xOf = (index: number, count: number) => (count > 1 ? (index / (count - 1)) * W : W / 2);

/** Scored points, kept so a lone step (a 1-step run, or one between two skips)
 *  is visible at all — a polyline of one point draws nothing. */
function dots(points: (number | null)[]): { x: number; y: number }[] {
  return points.flatMap((value, index) =>
    value === null || Number.isNaN(value)
      ? []
      : [{ x: xOf(index, points.length), y: y(value) }],
  );
}

function segments(points: (number | null)[]): string[] {
  const x = (i: number) => xOf(i, points.length);
  const out: string[] = [];
  let run: string[] = [];
  points.forEach((value, index) => {
    if (value === null || Number.isNaN(value)) {
      if (run.length > 0) out.push(run.join(" "));
      run = [];
      return;
    }
    run.push(`${x(index).toFixed(1)},${y(value).toFixed(1)}`);
  });
  if (run.length > 0) out.push(run.join(" "));
  return out;
}

/**
 * Validation-score curve over optimizer steps — hand-rolled inline SVG, the
 * same technique as the Observability charts (no chart library in this app).
 */
export function TrainCurve({ summary }: { summary: SkillLabTrainSummary }) {
  const { t } = useTranslation();
  const steps = summary.steps;
  const hard = steps.map((step) => step.selection_hard);
  const soft = steps.map((step) => step.selection_soft);
  const baseline = summary.baseline_selection_hard;
  const bestIndex = steps.findIndex((step) => step.step === summary.best_step);
  const bestX = xOf(bestIndex, steps.length);
  const bestScore = bestIndex >= 0 ? steps[bestIndex].selection_hard : null;

  if (steps.length === 0) {
    return (
      <div className="empty" data-testid="train-curve-empty">
        {t("skillLab.train.curve.waiting")}
      </div>
    );
  }

  return (
    <div className="obs-chart" data-testid="train-curve">
      <svg viewBox="0 0 560 130" style={{ width: "100%", height: 120 }}>
        <line x1="0" y1={BASE} x2={W} y2={BASE} stroke="#232B27" />
        <line x1="0" y1={y(0.5)} x2={W} y2={y(0.5)} stroke="#212823" strokeDasharray="3 4" />
        <line x1="0" y1={TOP} x2={W} y2={TOP} stroke="#212823" strokeDasharray="3 4" />
        <text x="4" y={TOP + 8}>
          1.0
        </text>
        <text x="4" y={BASE - 4}>
          0
        </text>
        {baseline !== null && (
          <line
            x1="0"
            y1={y(baseline)}
            x2={W}
            y2={y(baseline)}
            stroke="var(--ink-3)"
            strokeDasharray="5 4"
            strokeWidth="1.2"
          />
        )}
        {segments(soft).map((points, index) => (
          <polyline
            key={`soft-${index}`}
            fill="none"
            stroke="var(--s1)"
            strokeWidth="1.6"
            points={points}
          />
        ))}
        {segments(hard).map((points, index) => (
          <polyline
            key={`hard-${index}`}
            fill="none"
            stroke="var(--s3)"
            strokeWidth="1.6"
            points={points}
          />
        ))}
        {dots(soft).map((point, index) => (
          <circle key={`soft-dot-${index}`} cx={point.x} cy={point.y} r="1.8" fill="var(--s1)" />
        ))}
        {dots(hard).map((point, index) => (
          <circle key={`hard-dot-${index}`} cx={point.x} cy={point.y} r="1.8" fill="var(--s3)" />
        ))}
        {bestIndex >= 0 && bestScore !== null && (
          <>
            <line
              x1={bestX}
              y1={TOP}
              x2={bestX}
              y2={BASE}
              stroke="var(--good)"
              strokeWidth="1"
              strokeDasharray="2 3"
            />
            <circle cx={bestX} cy={y(bestScore)} r="3" fill="var(--good)" />
          </>
        )}
      </svg>
      <div className="obs-legend">
        <span>
          <i style={{ background: "var(--s3)" }} />
          {t("skillLab.train.curve.hard")}
        </span>
        <span>
          <i style={{ background: "var(--s1)" }} />
          {t("skillLab.train.curve.soft")}
        </span>
        {baseline !== null && (
          <span>
            <i style={{ background: "var(--ink-3)" }} />
            {t("skillLab.train.curve.baseline", { score: baseline.toFixed(3) })}
          </span>
        )}
        {summary.best_step !== null && (
          <span>
            <i style={{ background: "var(--good)" }} />
            {t("skillLab.train.curve.best", { step: summary.best_step })}
          </span>
        )}
        <span>{t("skillLab.train.curve.steps", { n: steps.length })}</span>
      </div>
    </div>
  );
}

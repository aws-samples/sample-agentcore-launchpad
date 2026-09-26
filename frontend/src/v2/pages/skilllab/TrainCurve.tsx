import { useTranslation } from "react-i18next";

import type { SkillLabTrainSummary } from "../../../lib/api";

const W = 760;
const H = 200;
const LEFT = 36;
const TOP = 12;
const BASE = 176;

/** Held-out gate scores are pass rates, so the axis is pinned to 0–1 rather
 *  than auto-scaled: a flat run should read as flat, not as full-height noise. */
const y = (value: number) => BASE - value * (BASE - TOP);
const xOf = (index: number, count: number) =>
  LEFT + (count > 1 ? (index / (count - 1)) * (W - LEFT - 12) : (W - LEFT - 12) / 2);

/** Contiguous runs of scored steps — a skipped step (null score) breaks the line
 *  instead of being drawn at zero. */
function segments(points: (number | null)[]): string[] {
  const out: string[] = [];
  let run: string[] = [];
  points.forEach((value, index) => {
    if (value === null || Number.isNaN(value)) {
      if (run.length > 0) out.push(run.join(" "));
      run = [];
      return;
    }
    run.push(`${xOf(index, points.length).toFixed(1)},${y(value).toFixed(1)}`);
  });
  if (run.length > 0) out.push(run.join(" "));
  return out;
}

/** Scored points, so a lone step (between two skips) is visible at all. */
const dots = (points: (number | null)[]) =>
  points.flatMap((value, index) =>
    value === null || Number.isNaN(value) ? [] : [{ x: xOf(index, points.length), y: y(value) }],
  );

const HARD = "var(--v2-primary)";
const SOFT = "#14c9c9";
const BASELINE = "var(--v2-ink-3)";
const BEST = "var(--v2-success)";

/** Validation-score curve over optimizer steps (inline SVG, V2 palette). */
export function TrainCurve({ summary }: { summary: SkillLabTrainSummary }) {
  const { t } = useTranslation();
  const steps = summary.steps;
  const hard = steps.map((step) => step.selection_hard);
  const soft = steps.map((step) => step.selection_soft);
  const baseline = summary.baseline_selection_hard;
  const bestIndex = steps.findIndex((step) => step.step === summary.best_step);
  const bestScore = bestIndex >= 0 ? steps[bestIndex].selection_hard : null;
  const bestX = xOf(bestIndex, steps.length);
  // at most ~16 x-axis labels, so a long run stays legible
  const labelEvery = Math.max(1, Math.ceil(steps.length / 16));
  if (steps.length === 0) return <p className="v2-muted">{t("skillLab.train.curve.waiting")}</p>;

  return (
    <div data-testid="v2-train-curve">
      <svg viewBox={`0 0 ${W} ${H}`} className="v2-skilllab-curve" role="img" aria-label={t("v2.skillLab.curve")}>
        {[0, 0.25, 0.5, 0.75, 1].map((v) => (
          <g key={v}>
            <line x1={LEFT} y1={y(v)} x2={W - 12} y2={y(v)} stroke="var(--v2-line)" strokeDasharray={v === 0 ? undefined : "3 4"} />
            <text x={LEFT - 6} y={y(v) + 4} textAnchor="end">
              {v.toFixed(v === 0 || v === 1 ? 0 : 2)}
            </text>
          </g>
        ))}
        {steps.map((step, index) =>
          index % labelEvery === 0 || index === steps.length - 1 ? (
            <text key={index} x={xOf(index, steps.length)} y={H - 6} textAnchor="middle">
              {step.step ?? index + 1}
            </text>
          ) : null,
        )}
        {baseline !== null && (
          <line x1={LEFT} y1={y(baseline)} x2={W - 12} y2={y(baseline)} stroke={BASELINE} strokeDasharray="6 4" strokeWidth="1.2" />
        )}
        {segments(soft).map((points, i) => (
          <polyline key={`s${i}`} fill="none" stroke={SOFT} strokeWidth="2" points={points} />
        ))}
        {segments(hard).map((points, i) => (
          <polyline key={`h${i}`} fill="none" stroke={HARD} strokeWidth="2" points={points} />
        ))}
        {dots(soft).map((p, i) => (
          <circle key={`sd${i}`} cx={p.x} cy={p.y} r="3" fill={SOFT} />
        ))}
        {dots(hard).map((p, i) => (
          <circle key={`hd${i}`} cx={p.x} cy={p.y} r="3" fill={HARD} />
        ))}
        {bestIndex >= 0 && bestScore !== null && (
          <>
            <line x1={bestX} y1={TOP} x2={bestX} y2={BASE} stroke={BEST} strokeDasharray="2 3" />
            <circle cx={bestX} cy={y(bestScore)} r="5" fill={BEST} />
          </>
        )}
      </svg>
      <div className="v2-skilllab-legend">
        <span>
          <i style={{ background: HARD }} />
          {t("skillLab.train.curve.hard")}
        </span>
        <span>
          <i style={{ background: SOFT }} />
          {t("skillLab.train.curve.soft")}
        </span>
        {baseline !== null && (
          <span>
            <i style={{ background: BASELINE }} />
            {t("skillLab.train.curve.baseline", { score: baseline.toFixed(3) })}
          </span>
        )}
        {summary.best_step !== null && (
          <span>
            <i style={{ background: BEST }} />
            {t("skillLab.train.curve.best", { step: summary.best_step })}
          </span>
        )}
        <span className="v2-muted">{t("skillLab.train.curve.steps", { n: steps.length })}</span>
      </div>
    </div>
  );
}

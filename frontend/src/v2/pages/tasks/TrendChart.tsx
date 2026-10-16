import { useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { ScorecardRun } from "../../../lib/api";
import { fmtScore } from "../../format";
import { type Band, isScored, previousValue, type RevisionMarker, shortDate, shortTime } from "../../scorecard";
import { groupLabelKey } from "../agents/snapshots";
import { DeltaChip } from "./scorecardUi";
import { runName, taskStatusOf } from "./scorecardText";

/** One line of the trend: a value per run (null breaks the line). */
export interface TrendSeries {
  key: string;
  label: string;
  /** a CSS colour (`var(--v2-series-N)`; the composite is ink) */
  color: string;
  /** the composite: 3 px, drawn on top */
  thick?: boolean;
  values: (number | null)[];
}

const NARROW = 560;
const MARGIN = { left: 40, top: 30, bottom: 50 };
const RIGHT_WIDE = 140;
const RIGHT_NARROW = 12;
const TIP_WIDTH = 248;
const TICKS = [0, 0.25, 0.5, 0.75, 1];
/** Minimum horizontal room per x-axis label before labels are thinned out. */
const LABEL_ROOM = 56;
/** Vertical gap between two direct end labels. */
const LABEL_GAP = 15;

/** Rough rendered width of chart text: CJK ≈ one em, everything else ≈ 0.56 em. */
function textWidth(text: string, size = 12): number {
  let w = 0;
  for (const ch of text) w += /[\u3000-\u9fff\uff00-\uffef]/.test(ch) ? size : size * 0.56;
  return w;
}

function clipText(text: string, max: number, size = 12): string {
  if (textWidth(text, size) <= max) return text;
  let out = "";
  for (const ch of text) {
    if (textWidth(`${out}${ch}…`, size) > max) break;
    out += ch;
  }
  return `${out}…`;
}

/** Contiguous runs of points: a missing value breaks the line instead of bridging it. */
function segments(values: (number | null)[], x: (i: number) => number, y: (v: number) => number): string[] {
  const out: string[] = [];
  let run: string[] = [];
  values.forEach((value, i) => {
    if (value == null) {
      if (run.length > 1) out.push(run.join(" "));
      run = [];
      return;
    }
    run.push(`${x(i).toFixed(1)},${y(value).toFixed(1)}`);
  });
  if (run.length > 1) out.push(run.join(" "));
  return out;
}

/** End labels sorted by y and pushed apart, kept inside [top, bottom]. */
function spread<T extends { y: number }>(labels: T[], top: number, bottom: number): T[] {
  const out = [...labels].sort((a, b) => a.y - b.y).map((l) => ({ ...l }));
  for (let i = 1; i < out.length; i += 1) if (out[i].y - out[i - 1].y < LABEL_GAP) out[i].y = out[i - 1].y + LABEL_GAP;
  if (out.length && out[out.length - 1].y > bottom) {
    out[out.length - 1].y = bottom;
    for (let i = out.length - 2; i >= 0; i -= 1) if (out[i + 1].y - out[i].y < LABEL_GAP) out[i].y = out[i + 1].y - LABEL_GAP;
  }
  for (const l of out) l.y = Math.max(top, l.y);
  return out;
}

/**
 * The benchmark trend (hand-rolled SVG, as TrainCurve): runs evenly spaced in order, a
 * fixed 0–1 normalized axis, one line per evaluator plus the thicker composite, version
 * bands labelled `#N` with what changed, a dashed marker where the dataset changed, and
 * pending runs as hollow markers. Hover shows a crosshair tooltip with each value and its
 * change from the previous point; a click picks a scored run for compare (the run table's
 * checkboxes are the keyboard path). The legend toggles series.
 */
export function TrendChart({
  runs,
  series,
  bands,
  markers,
  picked,
  onPick,
  title,
  describe,
}: {
  runs: ScorecardRun[];
  series: TrendSeries[];
  bands: Band[];
  markers: RevisionMarker[];
  picked: string[];
  onPick: (run: ScorecardRun) => void;
  /** the benchmark's name, for the summary label */
  title: string;
  /** a run's meta line in the tooltip (time · version · scope) */
  describe: (run: ScorecardRun) => string;
}) {
  const { t } = useTranslation();
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  const [hidden, setHidden] = useState<Set<string>>(() => new Set());
  const [hover, setHover] = useState<number | null>(null);

  // measured before paint, so the first frame is already the container's width
  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const measure = () => setWidth(el.clientWidth);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const toggle = (key: string) =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const W = Math.max(280, width || 720);
  const narrow = W < NARROW;
  const H = narrow ? 280 : 320;
  const right = narrow ? RIGHT_NARROW : RIGHT_WIDE;
  const iw = W - MARGIN.left - right;
  const ih = H - MARGIN.top - MARGIN.bottom;
  const bottom = MARGIN.top + ih;
  const n = runs.length;
  const step = iw / Math.max(1, n);
  const x = (i: number) => MARGIN.left + (i + 0.5) * step;
  const y = (v: number) => MARGIN.top + (1 - v) * ih;
  const shown = series.filter((s) => !hidden.has(s.key));
  // the composite last, so it sits on top of the evaluator lines
  const drawOrder = [...shown].sort((a, b) => Number(!!a.thick) - Number(!!b.thick));

  let lastScored = -1;
  runs.forEach((run, i) => {
    if (isScored(run)) lastScored = i;
  });
  const endX = x(n - 1) + 14;
  const ends = narrow || lastScored < 0
    ? []
    : spread(
        shown.flatMap((s) => {
          const v = s.values[lastScored];
          return v == null ? [] : [{ s, v, y: y(v) }];
        }),
        MARGIN.top,
        bottom,
      );
  const every = Math.max(1, Math.ceil(n / Math.max(1, Math.floor(iw / LABEL_ROOM))));
  const versioned = bands.filter((b) => b.seq != null);

  const head = series.find((s) => s.thick) ?? series[0];
  const summary = t("v2.scorecard.chart.aria", {
    bench: title,
    points: runs
      .map((run, i) =>
        isScored(run)
          ? t("v2.scorecard.chart.ariaPoint", { date: shortDate(run.created_at), series: head?.label ?? "", score: fmtScore(head?.values[i]) })
          : t("v2.scorecard.chart.ariaPending", { date: shortDate(run.created_at), status: t(`v2.taskStatus.${taskStatusOf(run.status)}`) }),
      )
      .join(t("v2.scorecard.chart.ariaSep")),
  });

  const tip = hover != null && runs[hover] ? runs[hover] : null;
  const tipWidth = Math.min(TIP_WIDTH, W);
  const tipLeft =
    hover == null
      ? 0
      : Math.max(0, Math.min(W - tipWidth, x(hover) + 14 + tipWidth > W ? x(hover) - 14 - tipWidth : x(hover) + 14));

  return (
    <div className="v2-trend" data-testid="v2-scorecard-chart">
      <div className="v2-trend-legend" data-testid="v2-scorecard-legend">
        {series.map((s) => (
          <button
            key={s.key}
            type="button"
            aria-pressed={!hidden.has(s.key)}
            onClick={() => toggle(s.key)}
            title={s.label}
            data-testid={`v2-scorecard-legend-${s.key}`}
          >
            <i className={s.thick ? "thick" : undefined} style={{ background: s.color }} />
            <span className="txt">{s.label}</span>
          </button>
        ))}
      </div>
      <div className="v2-trend-plot" ref={wrapRef} onPointerLeave={() => setHover(null)}>
        <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label={summary}>
          {/* version bands: alternating fill, `#N` and what changed when there is room */}
          {versioned.map((band, bi) => {
            const x0 = MARGIN.left + band.start * step;
            const x1 = MARGIN.left + (band.end + 1) * step;
            const bw = x1 - x0;
            const name = `${band.approx ? "≈ " : ""}#${band.seq}`;
            const changes = band.changes.map((g) => t(groupLabelKey(g))).join(" · ");
            const showChanges = !!changes && bw > textWidth(`${name} · ${changes}`) + 12;
            return (
              <g key={`band-${band.start}`} data-testid={`v2-scorecard-band-${band.seq}`}>
                {bi % 2 === 0 && <rect x={x0} y={MARGIN.top} width={bw} height={ih} fill="var(--v2-chart-band)" />}
                {bw >= 24 && (
                  <text x={x0 + 6} y={MARGIN.top - 10} className="band">
                    {name}
                    {showChanges && <tspan className="changes">{` · ${changes}`}</tspan>}
                  </text>
                )}
                {band.start > 0 && (
                  <line x1={x0} x2={x0} y1={MARGIN.top - 22} y2={bottom} stroke="var(--v2-line)" strokeWidth={1} />
                )}
              </g>
            );
          })}
          {/* grid + y labels (fixed 0–1: a flat run reads flat) */}
          {TICKS.map((v) => (
            <g key={v}>
              <line
                x1={MARGIN.left}
                x2={MARGIN.left + iw}
                y1={y(v)}
                y2={y(v)}
                stroke={v === 0 ? "var(--v2-ink-4)" : "var(--v2-chart-grid)"}
                strokeWidth={1}
              />
              <text x={MARGIN.left - 8} y={y(v) + 4} textAnchor="end" className="tick">
                {v === 0 ? "0" : v === 1 ? "1.0" : v}
              </text>
            </g>
          ))}
          {/* the dataset's content changed between two runs */}
          {markers.map((m) => {
            const bx = MARGIN.left + m.index * step;
            const label =
              m.fromSize != null && m.toSize != null && m.fromSize !== m.toSize
                ? t("v2.scorecard.chart.revisionSizes", { from: m.fromSize, to: m.toSize })
                : t("v2.scorecard.chart.revision");
            const flip = bx + 5 + textWidth(label, 11) > MARGIN.left + iw;
            return (
              <g key={`rev-${m.index}`} data-testid={`v2-scorecard-revision-${m.index}`}>
                <line x1={bx} x2={bx} y1={MARGIN.top} y2={bottom} stroke="var(--v2-ink-3)" strokeWidth={1} strokeDasharray="3 3" />
                <text x={flip ? bx - 5 : bx + 5} y={bottom - 7} textAnchor={flip ? "end" : "start"} className="note">
                  {label}
                </text>
              </g>
            );
          })}
          {/* picked runs */}
          {picked.map((id, pi) => {
            const i = runs.findIndex((run) => run.id === id);
            if (i < 0) return null;
            const w = Math.min(36, Math.max(8, step - 4));
            return (
              <g key={`pick-${id}`}>
                <rect x={x(i) - w / 2} y={MARGIN.top} width={w} height={ih} fill="var(--v2-primary)" fillOpacity={0.08} />
                <circle cx={x(i)} cy={MARGIN.top + 11} r={9} fill="var(--v2-ink)" />
                <text x={x(i)} y={MARGIN.top + 15} textAnchor="middle" className="ab">
                  {pi === 0 ? "A" : "B"}
                </text>
              </g>
            );
          })}
          {/* lines, then markers with a surface ring */}
          {drawOrder.map((s) => (
            <g key={`s-${s.key}`}>
              {segments(s.values, x, y).map((points, si) => (
                <polyline
                  key={si}
                  points={points}
                  fill="none"
                  stroke={s.color}
                  strokeWidth={s.thick ? 3 : 2}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                />
              ))}
              {s.values.map((v, i) =>
                v == null ? null : (
                  <circle key={i} cx={x(i)} cy={y(v)} r={s.thick ? 4.5 : 4} fill={s.color} stroke="var(--v2-card)" strokeWidth={2} />
                ),
              )}
            </g>
          ))}
          {/* runs still queued / replaying / judging */}
          {runs.map((run, i) =>
            isScored(run) || run.status === "completed" ? null : (
              <g key={`pending-${run.id}`} data-testid={`v2-scorecard-pending-${run.id}`}>
                <circle
                  cx={x(i)}
                  cy={y(0.5)}
                  r={7}
                  fill="var(--v2-card)"
                  stroke="var(--v2-primary)"
                  strokeWidth={2}
                  strokeDasharray="3 2"
                />
                <text x={x(i)} y={y(0.5) - 14} textAnchor="middle" className="note">
                  {t(`v2.taskStatus.${taskStatusOf(run.status)}`)}
                </text>
              </g>
            ),
          )}
          {/* direct labels at the line ends (dropped when narrow) */}
          {ends.map(({ s, v, y: ty }) => {
            const value = fmtScore(v);
            const room = W - (endX + 15) - textWidth(value) - 8;
            return (
              <g key={`end-${s.key}`}>
                <line x1={endX} x2={endX + 10} y1={ty} y2={ty} stroke={s.color} strokeWidth={s.thick ? 3 : 2} strokeLinecap="round" />
                <text x={endX + 15} y={ty + 4} className={s.thick ? "end thick" : "end"}>
                  {clipText(s.label, room)} <tspan className="val">{value}</tspan>
                </text>
              </g>
            );
          })}
          {/* x labels: date + time, thinned out from the newest run backwards */}
          {runs.map((run, i) =>
            (n - 1 - i) % every === 0 ? (
              <g key={`x-${run.id}`}>
                <text x={x(i)} y={bottom + 18} textAnchor="middle" className="x">
                  {shortDate(run.created_at)}
                </text>
                <text x={x(i)} y={bottom + 34} textAnchor="middle" className="tick">
                  {shortTime(run.created_at)}
                </text>
              </g>
            ) : null,
          )}
          {hover != null && (
            <line x1={x(hover)} x2={x(hover)} y1={MARGIN.top} y2={bottom} stroke="var(--v2-ink-3)" strokeWidth={1} pointerEvents="none" />
          )}
          {/* hit targets: one full-height column per run, wider than any mark */}
          {runs.map((run, i) => (
            <rect
              key={`hit-${run.id}`}
              x={MARGIN.left + i * step}
              y={MARGIN.top - 24}
              width={step}
              height={ih + 24 + MARGIN.bottom}
              fill="transparent"
              className={isScored(run) ? "hit pick" : "hit"}
              onPointerEnter={() => setHover(i)}
              onPointerDown={() => setHover(i)}
              onClick={() => {
                if (isScored(run)) onPick(run);
              }}
              data-testid={`v2-scorecard-point-${run.id}`}
            />
          ))}
        </svg>
        {tip && hover != null && (
          <div className="v2-trend-tip" style={{ left: tipLeft, top: MARGIN.top, width: tipWidth }} role="presentation">
            <div className="h">{runName(tip)}</div>
            <div className="m">{describe(tip)}</div>
            {isScored(tip) ? (
              shown.map((s) => {
                const v = s.values[hover];
                if (v == null) return null;
                const p = previousValue(s.values, hover);
                return (
                  <div className="r" key={s.key}>
                    <i className={s.thick ? "thick" : undefined} style={{ background: s.color }} />
                    <span className="n">{s.label}</span>
                    <b>{fmtScore(v)}</b>
                    {p != null ? <DeltaChip value={v - p} /> : <span className="v2-delta none">{t("v2.scorecard.chart.first")}</span>}
                  </div>
                );
              })
            ) : (
              <div className="r muted">
                {tip.status === "completed"
                  ? t("v2.scorecard.chart.noScores")
                  : t("v2.scorecard.chart.pending", { status: t(`v2.taskStatus.${taskStatusOf(tip.status)}`) })}
              </div>
            )}
            {isScored(tip) && (
              <div className="f">{t(picked.includes(tip.id) ? "v2.scorecard.chart.unpick" : "v2.scorecard.chart.pick")}</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

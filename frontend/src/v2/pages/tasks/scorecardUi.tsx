// Small display pieces of the evaluation scorecard views (the two-run compare page, the
// agent list and the agent page): the agent version a run evaluated, a delta chip, an
// "A → B" score pair and the list's composite sparkline. Scores arrive already
// normalized (higher is better).
import { useTranslation } from "react-i18next";

import type { RunSnapshotRef } from "../../../lib/api";
import { delta, type DeltaTone, deltaTone } from "../../compare";
import { fmtScore } from "../../format";
import { Tag } from "../../ui";

/** `#N`, with a marker when the version was inferred (an older run recorded none) or
 *  observed (a passive run scores sessions that predate it); "no version" for a
 *  log-source run or an agent without a deployed snapshot. */
export function VersionTag({ snapshot, testId }: { snapshot: RunSnapshotRef | null | undefined; testId?: string }) {
  const { t } = useTranslation();
  if (!snapshot) {
    return (
      <Tag tone="gray" title={t("v2.compare.version.noneHint")} testId={testId}>
        {t("v2.compare.version.none")}
      </Tag>
    );
  }
  const { seq, source } = snapshot;
  return (
    <span className={`v2-ver-tag ${source}`}>
      <Tag tone="outline" title={t(`v2.compare.version.${source}Hint`, { seq })} testId={testId}>
        {t(`v2.compare.version.${source}`, { seq })}
      </Tag>
    </span>
  );
}

/** A change chip: ▲ / ▼ and the size (never colour alone), "≈ flat" inside ±0.02. */
export function DeltaChip({ value, testId }: { value: number | null; testId?: string }) {
  const { t } = useTranslation();
  if (value == null) {
    return (
      <span className="v2-delta none" data-testid={testId}>
        —
      </span>
    );
  }
  const tone = deltaTone(value);
  if (tone === "flat") {
    return (
      <span className="v2-delta flat" title={t("v2.compare.flatHint")} data-testid={testId}>
        {t("v2.compare.flat")}
      </span>
    );
  }
  return (
    <span className={`v2-delta ${tone}`} data-testid={testId}>
      {tone === "up" ? "▲" : "▼"} {Math.abs(value).toFixed(2)}
    </span>
  );
}

/** A 96 × 28 composite sparkline (the agent list's 趋势); "—" below two points. */
export function Sparkline({ values, label, testId }: { values: number[]; label: string; testId?: string }) {
  const w = 96;
  const h = 28;
  const n = values.length;
  if (n < 2) {
    return (
      <span className="v2-muted" data-testid={testId}>
        —
      </span>
    );
  }
  const x = (i: number) => 3 + (i * (w - 6)) / (n - 1);
  // fixed 0–1, as the trend: a flat history reads flat
  const y = (v: number) => 3 + (1 - v) * (h - 6);
  const line = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `M${x(0).toFixed(1)},${h - 1} L${line.split(" ").join(" L")} L${x(n - 1).toFixed(1)},${h - 1} Z`;
  return (
    <svg className="v2-spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} role="img" aria-label={label} data-testid={testId}>
      <path d={area} fill="var(--v2-primary-soft)" />
      <polyline points={line} fill="none" stroke="var(--v2-primary)" strokeWidth={1.6} strokeLinejoin="round" />
      <circle cx={x(n - 1)} cy={y(values[n - 1])} r={3} fill="var(--v2-primary)" stroke="var(--v2-card)" strokeWidth={1.5} />
    </svg>
  );
}

/** "0.62 → 0.81", B tinted by `tone`, optionally followed by the delta chip. */
export function ScorePair({
  a,
  b,
  chip,
  tone,
}: {
  a: number | null;
  b: number | null;
  chip?: boolean;
  tone?: Exclude<DeltaTone, "flat">;
}) {
  return (
    <span className="v2-score-pair">
      <span className="a">{fmtScore(a)}</span>
      <span className="arr" aria-hidden="true">
        →
      </span>
      <span className={tone ? `b ${tone}` : "b"}>{fmtScore(b)}</span>
      {chip && <DeltaChip value={delta(a, b)} />}
    </span>
  );
}

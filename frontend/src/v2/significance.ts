// EVAL-TRUST 3.2: is a difference between two runs real? The twin of the backend's
// `evaluation/run_stats.compare` (pinned to it in significance.test.ts): one observation per
// session, a difference is `significant` only when the two 95 % intervals do not overlap AND
// Welch's two-sided p < 0.05; `not_significant` otherwise; `insufficient` when a side has
// fewer than two sessions (or a run scored before the interval existed). No DOM.
import type { EvaluationScore } from "../lib/evaluation";

export type Significance = "significant" | "not_significant" | "insufficient";

const ALPHA = 0.05;
const MAX_ITER = 300;
const EPS = 3.0e-14;
const FPMIN = 1.0e-300;
const MEAN_TOLERANCE = 1e-9;
const VARIANCE_TOLERANCE = 1e-12;

function lnGamma(z: number): number {
  // Lanczos approximation (g = 7, n = 9)
  const c = [0.99999999999980993, 676.5203681218851, -1259.1392167224028, 771.32342877765313, -176.61502916214059, 12.507343278686905, -0.13857109526572012, 9.9843695780195716e-6, 1.5056327351493116e-7];
  if (z < 0.5) return Math.log(Math.PI / Math.sin(Math.PI * z)) - lnGamma(1 - z);
  const x = z - 1;
  let a = c[0];
  const t = x + 7.5;
  for (let i = 1; i < 9; i += 1) a += c[i] / (x + i);
  return 0.5 * Math.log(2 * Math.PI) + (x + 0.5) * Math.log(t) - t + Math.log(a);
}

function betacf(a: number, b: number, x: number): number {
  const qab = a + b;
  const qap = a + 1;
  const qam = a - 1;
  let c = 1;
  let d = 1 - (qab * x) / qap;
  if (Math.abs(d) < FPMIN) d = FPMIN;
  d = 1 / d;
  let h = d;
  for (let m = 1; m <= MAX_ITER; m += 1) {
    const m2 = 2 * m;
    let aa = (m * (b - m) * x) / ((qam + m2) * (a + m2));
    d = 1 + aa * d;
    if (Math.abs(d) < FPMIN) d = FPMIN;
    c = 1 + aa / c;
    if (Math.abs(c) < FPMIN) c = FPMIN;
    d = 1 / d;
    h *= d * c;
    aa = (-(a + m) * (qab + m) * x) / ((a + m2) * (qap + m2));
    d = 1 + aa * d;
    if (Math.abs(d) < FPMIN) d = FPMIN;
    c = 1 + aa / c;
    if (Math.abs(c) < FPMIN) c = FPMIN;
    d = 1 / d;
    const del = d * c;
    h *= del;
    if (Math.abs(del - 1) < EPS) break;
  }
  return h;
}

function incompleteBeta(a: number, b: number, x: number): number {
  if (x <= 0) return 0;
  if (x >= 1) return 1;
  const bt = Math.exp(lnGamma(a + b) - lnGamma(a) - lnGamma(b) + a * Math.log(x) + b * Math.log(1 - x));
  return x < (a + 1) / (a + b + 2) ? (bt * betacf(a, b, x)) / a : 1 - (bt * betacf(b, a, 1 - x)) / b;
}

/** P(|T| ≥ |t|) for Student-t with `df` degrees of freedom. */
export function studentTTwoSidedP(t: number, df: number): number {
  if (!Number.isFinite(t)) return 0;
  if (t === 0) return 1;
  return Math.min(1, Math.max(0, incompleteBeta(df / 2, 0.5, df / (df + t * t))));
}

type Stats = Pick<EvaluationScore, "sessions_n" | "session_mean" | "sd" | "ci95"> & Partial<Pick<EvaluationScore, "scale" | "evaluatorId">>;

const Z95 = 1.959963984540054;

/** N17-obs423: the 95 % Wilson interval of a proportion (the backend's binary-evaluator
 *  interval, `run_stats.wilson`). */
export function wilson(successes: number, n: number): [number, number] {
  const p = successes / n;
  const z2 = Z95 * Z95;
  const centre = (p + z2 / (2 * n)) / (1 + z2 / n);
  const half = (Z95 * Math.sqrt((p * (1 - p)) / n + z2 / (4 * n * n))) / (1 + z2 / n);
  return [Math.max(0, centre - half), Math.min(1, centre + half)];
}

/** N17-obs423: an entry's interval inside its scale — the stored `scale`, else [0, 1] for a
 *  built-in evaluator (a row summarised before the clamp may still read [0.263, 1.166]);
 *  unchanged when the scale is unknown. */
export function boundedInterval(entry: Stats | undefined): [number, number] | null {
  const ci = entry?.ci95;
  if (!ci) return null;
  const scale = entry?.scale ?? (entry?.evaluatorId?.startsWith("Builtin.") ? ([0, 1] as [number, number]) : null);
  if (!scale) return ci;
  return [Math.max(scale[0], ci[0]), Math.min(scale[1], ci[1])];
}

/** Welch's two-sided p of B − A from the stored summaries; null below two sessions a side. */
export function welchP(a: Stats, b: Stats): number | null {
  const na = a.sessions_n ?? 0;
  const nb = b.sessions_n ?? 0;
  if (na < 2 || nb < 2 || a.session_mean == null || b.session_mean == null) return null;
  const diff = b.session_mean - a.session_mean;
  const va = (a.sd ?? 0) ** 2;
  const vb = (b.sd ?? 0) ** 2;
  if (Math.abs(diff) < MEAN_TOLERANCE) return 1;
  if (va < VARIANCE_TOLERANCE && vb < VARIANCE_TOLERANCE) return 0;
  const sa = va / na;
  const sb = vb / nb;
  const se2 = sa + sb;
  const t = diff / Math.sqrt(se2);
  const denom = (sa ? sa ** 2 / (na - 1) : 0) + (sb ? sb ** 2 / (nb - 1) : 0);
  const df = denom > 0 ? se2 ** 2 / denom : na + nb - 2;
  return studentTTwoSidedP(t, df);
}

/** The label of one evaluator's difference between two runs' score entries. Reads the raw
 *  clamped intervals: both sides are the same evaluator, so the N17-obs430 polarity mirror
 *  (`format.normalizedInterval`, the displayed form) leaves their overlap unchanged. */
export function significance(a: Stats | undefined, b: Stats | undefined): Significance {
  const ca = boundedInterval(a);
  const cb = boundedInterval(b);
  if (!a || !b || !ca || !cb) return "insufficient";
  const p = welchP(a, b); // unchanged by the clamp: Welch reads the plain SD (N17-obs423)
  if (p == null) return "insufficient";
  const overlap = ca[0] <= cb[1] && cb[0] <= ca[1];
  return !overlap && p < ALPHA ? "significant" : "not_significant";
}

/** One evaluator's score entry of a run, or undefined. */
export function scoreEntry(run: { scores: EvaluationScore[] }, evaluatorId: string): EvaluationScore | undefined {
  return run.scores.find((s) => s.evaluatorId === evaluatorId);
}

/** Over several evaluators (a composite): significant when any one is, not significant when
 *  every comparable one is not, insufficient when none can be compared. */
export function combined(labels: Significance[]): Significance {
  if (labels.includes("significant")) return "significant";
  return labels.includes("not_significant") ? "not_significant" : "insufficient";
}

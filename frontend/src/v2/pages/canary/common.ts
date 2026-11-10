// Runtime canaries (champion vs candidate version behind a dedicated gateway,
// ramped 90/10 → 50/50 → 1/99 on real production traffic) — shared rules.
import type { TFunction } from "i18next";

import type { AgentInfo, RuntimeCanaryInfo } from "../../../lib/api";
import type { Option, TagTone } from "../../ui";

export const RAMP_STAGES = [
  { control: 90, treatment: 10 },
  { control: 50, treatment: 50 },
  { control: 1, treatment: 99 },
] as const;

/** A Harness canary may `complete` from this ramp stage (50/50) on a treatment-wins or
 *  tie verdict — mirrors `canary_service.EARLY_COMPLETE_STAGE` / `EARLY_COMPLETE_VERDICTS`
 *  (a tie or a non-significant win still needs the override). It may also open here. */
export const EARLY_COMPLETE_STAGE = 1;
export const EARLY_COMPLETE_VERDICTS: readonly string[] = ["treatment-wins", "tie"];

/** `C/T1` of the stage a new canary opens at. */
export function openingWeights(runFirst: boolean): string {
  const r = RAMP_STAGES[runFirst ? 0 : EARLY_COMPLETE_STAGE];
  return `${r.control}/${r.treatment}`;
}

export const CANARY_TONE: Record<RuntimeCanaryInfo["status"], TagTone> = {
  running: "blue",
  completed: "green",
  rolled_back: "red",
  cleaned: "gray",
};

export function verdictTone(verdict: string): TagTone {
  if (verdict === "treatment-wins") return "green";
  if (verdict === "control-wins") return "red";
  if (verdict.includes("insufficient") || verdict === "tie") return "orange";
  if (verdict === "judge-mismatch") return "red";
  return "gray";
}

type Verdict = NonNullable<NonNullable<RuntimeCanaryInfo["artifacts"]["rounds"]>[number]["verdict"]>;

/** N17-obs443: every verdict code `verdict.compute_verdict` / `act_verdict` emit — each has a
 *  display name (`canaryVerdict.code.<code>`); the raw code rides along as a tooltip. */
export const VERDICT_CODES = [
  "treatment-wins",
  "control-wins",
  "tie",
  "insufficient-n",
  "insufficient-data",
  "judge-mismatch",
] as const;

/** N17-obs443: the stable `reason_code`s the backend sends with `reason_params`
 *  (`backend/tests/optimization/test_verdict_reason_codes.py` pins this list). */
export const VERDICT_REASON_CODES = [
  "no_metrics",
  "no_means",
  "arm_n_below_bar",
  "primary_arm_n_below_bar",
  "judge_mismatch",
  "primary_no_scores",
  "design_no_primary",
  "design_below_planned_n",
  "primary_no_p_value",
  "not_significant",
  "no_new_samples",
  "no_new_primary_samples",
] as const;

/** N17-obs447: every canary action progress code (`backend/app/optimization/progress_steps.py`
 *  `STEP_CODES`; `test_canary_progress_codes.py` pins this list and the locale keys). */
export const CANARY_PROGRESS_CODES = [
  "creating_online_config",
  "stopping_ab_test",
  "reading_current_version",
  "minting_candidate",
  "opening_ab_test",
  "replaying_sessions",
  "aggregating_evidence",
  "shifting_traffic",
  "recording_promotion",
  "rolling_forward",
  "deleting_artifact",
  "tearing_down",
  "waiting_endpoints_gone",
  "current_version",
  "candidate_artifact",
  "candidate_published",
  "candidate_runtime_status",
  "pinning_endpoint",
  "endpoint_status",
  "promoting_stable_endpoint",
  "stable_endpoint_status",
  "runtime_gone",
  "endpoint_listing_failed",
  "endpoints_still_listed",
  "endpoint_already_gone",
  "dispatching_replay",
  "worker_progress",
] as const;

/** The running action's line in the console language from `progress_code` + `progress_params`;
 *  a plain-text line (a shared deployer helper's), an older payload or an unknown code falls
 *  back to the server's `progress` text. */
export function progressText(
  canary: Pick<RuntimeCanaryInfo, "progress" | "progress_code" | "progress_params">,
  t: TFunction,
): string | null {
  const code = canary.progress_code;
  if (!code || !(CANARY_PROGRESS_CODES as readonly string[]).includes(code)) return canary.progress ?? null;
  const params: Record<string, unknown> = { ...(canary.progress_params ?? {}) };
  if (params.role === "control") params.role = t("v2.canary.champion");
  if (params.role === "treatment") params.role = t("v2.canary.candidate");
  if (Array.isArray(params.endpoints)) params.endpoints = params.endpoints.join(", ");
  return t(`canaryProgress.${code}`, params);
}

/** N17-obs451: the completion / rollback summary, worded by the verdict the canary was
 *  promoted on. `artifacts.complete` records it (`verdict` / `significant` / `override`); an
 *  older row reads the verdict of the round it completed at (else its last verdict), and the
 *  override from the same rule the server applies (`assert_verdict_allows`). `complete` only
 *  ever succeeds on a `treatment-wins` or, with the override, a `tie` / non-significant
 *  result; a verdict it cannot see words the promotion without claiming a winner. */
export function completionSummary(canary: RuntimeCanaryInfo, t: TFunction): { tone: "success" | "warn"; text: string } | null {
  const a = canary.artifacts ?? {};
  if (a.complete) {
    const version = a.complete.promoted_version ?? "—";
    const rounds = a.rounds ?? [];
    const atStage = rounds.find((r) => r.ramp_stage === a.complete?.completed_at_stage)?.verdict;
    const lastVerdict = [...rounds].reverse().find((r) => r.verdict)?.verdict;
    const verdict = a.complete.verdict ?? atStage?.verdict ?? lastVerdict?.verdict ?? null;
    const significant = a.complete.significant ?? atStage?.significant ?? lastVerdict?.significant;
    const override = a.complete.override ?? (verdict === "tie" || verdict === "insufficient-n" || significant === false);
    if (verdict === "treatment-wins" && !override) {
      return { tone: "success", text: t(significant === true ? "canaryPage.completed.won" : "canaryPage.completed.wonUnrated", { version }) };
    }
    if (verdict === "treatment-wins") return { tone: "warn", text: t("canaryPage.completed.notSignificant", { version }) };
    if (verdict) {
      return { tone: "warn", text: t("canaryPage.completed.override", { version, verdict: verdictLabel(verdict, t) }) };
    }
    return { tone: "warn", text: t("canaryPage.completed.unknown", { version }) };
  }
  if (a.rollback) return { tone: "warn", text: t("canaryPage.rollbackSummary", { version: a.rollback.restored_version ?? "—" }) };
  if (canary.status === "cleaned" && a.setup) return { tone: "warn", text: t("canaryPage.cleanedWithoutDecision") };
  return null;
}

/** The verdict's display name; an unknown (newer) code shows as itself. */
export function verdictLabel(code: string, t: TFunction): string {
  return (VERDICT_CODES as readonly string[]).includes(code) ? t(`canaryVerdict.code.${code}`) : code;
}

/** The verdict's reason worded in the console language from `reason_code` + `reason_params`;
 *  an older payload (or an unknown code) falls back to the server's English `reason`.
 *  Evaluator ids are named through `nameOf`. */
export function verdictReason(
  verdict: Verdict | undefined,
  t: TFunction,
  nameOf: (id: string) => string = (id) => id,
): string | null {
  if (!verdict) return null;
  const code = verdict.reason_code;
  if (!code || !(VERDICT_REASON_CODES as readonly string[]).includes(code)) return verdict.reason ?? null;
  const params: Record<string, unknown> = { ...(verdict.reason_params ?? {}) };
  if (typeof params.primary === "string") params.primary = nameOf(params.primary);
  if (Array.isArray(params.evaluators)) params.evaluators = params.evaluators.map((e) => nameOf(String(e))).join(", ");
  if (Array.isArray(params.judges)) params.judges = params.judges.join(", ");
  return t(`canaryVerdict.reason.${code}`, params);
}

/** The secondary-evaluator note, worded from `multiple_comparisons_k` (else the server text). */
export function multipleComparisonsNote(verdict: Verdict | undefined, t: TFunction): string | null {
  if (!verdict?.multiple_comparisons) return null;
  return verdict.multiple_comparisons_k != null
    ? t("canaryVerdict.multipleComparisons", { k: verdict.multiple_comparisons_k })
    : verdict.multiple_comparisons;
}

export type RampAction = "advance" | "complete";

/** Mirror of `canary_service.assert_verdict_allows` (N-SCN-obs14 / N-SCN-obs15): a
 *  significant `control-wins` and `insufficient-data` end the stage; `tie`,
 *  `insufficient-n` (an arm below the per-arm bar), a non-significant win AND a
 *  non-significant `control-wins` (`significant === false` — noise like a tie; ruling
 *  2026-10-14) advance only with the explicit operator override. A `control-wins` without
 *  a `significant` flag (older payload) stays terminal. `complete` is the terminal policy
 *  (`terminal=True`): promoting to 100 % on an arm below the bar is exactly the evidence
 *  the bar declares unusable, so `insufficient-n` is blocked there even with the override
 *  — unlike an experiment's `promote --force` — and ANY `control-wins` is blocked (the
 *  current version wins, there is nothing to promote). */
export function verdictGate(
  verdict: Verdict | undefined,
  action: RampAction = "advance",
): { blocked: boolean; needsOverride: boolean } {
  if (!verdict) return { blocked: false, needsOverride: false };
  const blocked =
    (verdict.verdict === "control-wins" && (action === "complete" || verdict.significant !== false)) ||
    verdict.verdict === "insufficient-data" ||
    // P3.2: arms scored by different judge models are never compared
    verdict.verdict === "judge-mismatch" ||
    (action === "complete" && verdict.verdict === "insufficient-n");
  const needsOverride =
    !blocked && (verdict.verdict === "tie" || verdict.verdict === "insufficient-n" || verdict.significant === false);
  return { blocked, needsOverride };
}

/** "scored records per version · current 32/10 · candidate 4/10 — send another traffic round"
 *  (N17-obs446: the versions by name; the arm keys C / T1 are the line's tooltip) for an
 *  `insufficient-n` verdict that carries the per-arm counts (`arms_n` / `min_arm_n`);
 *  at the `complete` stage the copy says promoting needs both arms at the bar (no
 *  override). Null for every other verdict or an older payload without the counts. */
export function insufficientArmHint(
  verdict: Verdict | undefined,
  t: TFunction,
  action: RampAction = "advance",
): string | null {
  if (!verdict || verdict.verdict !== "insufficient-n" || !verdict.arms_n || verdict.min_arm_n == null) return null;
  return t(action === "complete" ? "canaryPage.insufficientArmHintComplete" : "canaryPage.insufficientArmHint", {
    control: verdict.arms_n.control,
    treatment: verdict.arms_n.treatment,
    min: verdict.min_arm_n,
  });
}

type Round = NonNullable<RuntimeCanaryInfo["artifacts"]["rounds"]>[number];

/** `RuntimeCanaryAction.sessions_per_arm` bounds (`canary_routers.py`). */
export const SESSIONS_PER_ARM_MAX = 500;

/** N17-obs427: the traffic round's default `sessions_per_arm` — what the fixed-n design still
 *  needs on the thinner arm of this stage: `planned_n_per_arm` minus the scored sessions of the
 *  latest verdict (`arms_n`), else minus the sessions the round's attempts sent per arm.
 *  `null` without a design (one dataset pass, the server default). */
export function remainingPerArm(
  design: { planned_n_per_arm: number } | undefined,
  round: Round | undefined,
): number | null {
  const planned = design?.planned_n_per_arm;
  if (!planned || planned < 1) return null;
  let done = 0;
  if (round?.verdict?.arms_n) {
    done = Math.min(round.verdict.arms_n.control, round.verdict.arms_n.treatment);
  } else {
    const sent: Record<string, number> = {};
    for (const attempt of round?.traffic_attempts ?? []) {
      for (const [arm, n] of Object.entries(attempt.arms ?? {})) sent[arm] = (sent[arm] ?? 0) + n;
    }
    const counts = Object.values(sent);
    done = counts.length >= 2 ? Math.min(...counts) : 0;
  }
  return Math.min(SESSIONS_PER_ARM_MAX, Math.max(1, planned - done));
}

type Setup = RuntimeCanaryInfo["artifacts"]["setup"];

/** `v<current> → v<candidate>`, "…" for what the artifact has not reached yet. */
export function versionsLabel(setup: Setup): string {
  if (!setup) return "—";
  const v = (value?: string) => (value ? `v${value}` : "…");
  return `${v(setup.v_current)} → ${v(setup.v_candidate)}`;
}

/** Harness canary control choices: every version older than the latest, the first marked. */
export function versionOptions(earlier: string[], firstSuffix: string): Option[] {
  return earlier.map((v, i) => ({ value: v, label: `v${v}${i === 0 ? firstSuffix : ""}` }));
}

export function weightsLabel(setup: Setup): string {
  const w = setup?.weights;
  return w ? `${w.C ?? "—"}/${w.T1 ?? "—"}` : "—";
}

/** The rule 新建金丝雀 offers a champion by: an active agent the server marks canary-eligible
 *  (`canary_capability`; absent ⇒ ineligible, N12-bug2). */
export function canaryEligible(agent: Pick<AgentInfo, "status" | "canary_capability"> | null | undefined): boolean {
  return !!agent && agent.status === "active" && !!agent.canary_capability?.eligible;
}

// N17-obs309: a prompt recommendation hands its prompt to 新建金丝雀 through sessionStorage
// keyed by the recommendation id — a 20 000-character prompt does not belong in a URL that
// a reload sends to the CDN. The URL carries only `champion=` and `rec=`.
const SEED_PREFIX = "launchpad.canary-seed:";

/** Where 「用金丝雀验证」 goes: 新建金丝雀 with the run's agent as champion. */
export function canaryHandoffHref(agentId: string, recId: string): string {
  const q = new URLSearchParams({ canary: "new", champion: agentId, rec: recId });
  return `/v2/eval/experiments?${q.toString()}`;
}

export function stashCanarySeed(recId: string, prompt: string): void {
  try {
    window.sessionStorage.setItem(SEED_PREFIX + recId, prompt);
  } catch {
    // storage unavailable (private mode quota): the form still opens on the champion's prompt
  }
}

/** The candidate prompt a hand-off left for `recId`, or null (kept, so a reload re-seeds). */
export function readCanarySeed(recId: string | null): string | null {
  if (!recId) return null;
  try {
    return window.sessionStorage.getItem(SEED_PREFIX + recId);
  } catch {
    return null;
  }
}

// ─── R3-d: the power calculation shown before start ──────────────────────────────────
// A port of `optimization/stats.normal_quantile` / `sessions_per_arm` (the server
// recomputes the stored planned n; common.test.ts pins both to the same table).
const QA = [-3.969683028665376e1, 2.209460984245205e2, -2.759285104469687e2,
  1.38357751867269e2, -3.066479806614716e1, 2.506628277459239];
const QB = [-5.447609879822406e1, 1.615858368580409e2, -1.556989798598866e2,
  6.680131188771972e1, -1.328068155288572e1];
const QC = [-7.784894002430293e-3, -3.223964580411365e-1, -2.400758277161838,
  -2.549732539343734, 4.374664141464968, 2.938163982698783];
const QD = [7.784695709041462e-3, 3.224671290700398e-1, 2.445134137142996, 3.754408661907416];

function erfc(x: number): number {
  // Numerical Recipes erfc (fractional error < 1.2e-7) — enough for one Halley step
  const z = Math.abs(x);
  const t = 1 / (1 + 0.5 * z);
  const r = t * Math.exp(-z * z - 1.26551223 + t * (1.00002368 + t * (0.37409196 + t * (0.09678418
    + t * (-0.18628806 + t * (0.27886807 + t * (-1.13520398 + t * (1.48851587
    + t * (-0.82215223 + t * 0.17087277)))))))));
  return x >= 0 ? r : 2 - r;
}

export function normalQuantile(p: number): number {
  if (!(p > 0 && p < 1)) throw new Error("p must be in (0, 1)");
  const low = 0.02425;
  let x: number;
  if (p < low) {
    const q = Math.sqrt(-2 * Math.log(p));
    x = (((((QC[0] * q + QC[1]) * q + QC[2]) * q + QC[3]) * q + QC[4]) * q + QC[5])
      / ((((QD[0] * q + QD[1]) * q + QD[2]) * q + QD[3]) * q + 1);
  } else if (p <= 1 - low) {
    const q = p - 0.5;
    const r = q * q;
    x = (((((QA[0] * r + QA[1]) * r + QA[2]) * r + QA[3]) * r + QA[4]) * r + QA[5]) * q
      / (((((QB[0] * r + QB[1]) * r + QB[2]) * r + QB[3]) * r + QB[4]) * r + 1);
  } else {
    const q = Math.sqrt(-2 * Math.log(1 - p));
    x = -(((((QC[0] * q + QC[1]) * q + QC[2]) * q + QC[3]) * q + QC[4]) * q + QC[5])
      / ((((QD[0] * q + QD[1]) * q + QD[2]) * q + QD[3]) * q + 1);
  }
  const e = 0.5 * erfc(-x / Math.SQRT2) - p;
  const u = e * Math.sqrt(2 * Math.PI) * Math.exp((x * x) / 2);
  return x - u / (1 + (x * u) / 2);
}

export interface CanaryDesignInput { mde: number; sd: number; alpha: number; power: number }

/** Sessions per arm the fixed-n test needs; `null` for a meaningless input. */
export function sessionsPerArm({ mde, sd, alpha, power }: CanaryDesignInput): number | null {
  if (!(mde > 0 && mde <= 1) || !(sd > 0 && sd <= 1) || !(alpha > 0 && alpha <= 0.5)
    || !(power >= 0.5 && power < 1)) return null;
  const z = normalQuantile(1 - alpha / 2) + normalQuantile(power);
  return Math.max(2, Math.ceil((2 * z * z * sd * sd) / (mde * mde)));
}


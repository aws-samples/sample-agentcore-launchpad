import type { TFunction } from "i18next";

// Configuration-bundle experiments (recommend → bundles → gateway/A-B → traffic →
// verdict → promote → cleanup): artifact types and the per-experiment console
// state both consoles share — the classic experiment page and console V2.

export const DEFAULT_TRACE_LOOKBACK_HOURS = 24;
export const TRACE_LOOKBACK_OPTIONS = [24, 72, 168, 720] as const;

// The RECOMMEND generator checkboxes exist only before the stage runs, so the
// backend has nothing to restore them from — persist per experiment locally.
export const REC_TYPES_KEY_PREFIX = "launchpad.exp-rec-types.";

export function loadRecTypes(expId: string): { sp: boolean; td: boolean } {
  try {
    const raw = localStorage.getItem(REC_TYPES_KEY_PREFIX + expId);
    if (raw) {
      const parsed = JSON.parse(raw) as { sp?: boolean; td?: boolean };
      return { sp: !!parsed.sp, td: !!parsed.td };
    }
  } catch {
    /* corrupt or unavailable storage falls back to defaults */
  }
  return { sp: false, td: false };
}

export function saveRecTypes(expId: string, sp: boolean, td: boolean) {
  try {
    localStorage.setItem(REC_TYPES_KEY_PREFIX + expId, JSON.stringify({ sp, td }));
  } catch {
    /* storage unavailable — selection simply won't survive a reload */
  }
}

// The RECOMMEND pickers (trace source, optimizer, reflection model) are page
// state too — nothing server-side holds them before the stage runs, and after a
// run the artifact only records what WAS used. Persist them per experiment so
// re-opening an in-progress experiment shows the operator's own selection.
export const REC_PREFS_KEY_PREFIX = "launchpad.exp-rec-prefs.";

export interface RecPrefs {
  source?: string;
  provider?: string;
  model?: string;
  customModel?: string;
}

export function loadRecPrefs(expId: string): RecPrefs {
  try {
    const raw = localStorage.getItem(REC_PREFS_KEY_PREFIX + expId);
    if (raw) {
      const parsed = JSON.parse(raw) as RecPrefs;
      return {
        source: typeof parsed.source === "string" ? parsed.source : undefined,
        provider: typeof parsed.provider === "string" ? parsed.provider : undefined,
        model: typeof parsed.model === "string" ? parsed.model : undefined,
        customModel: typeof parsed.customModel === "string" ? parsed.customModel : undefined,
      };
    }
  } catch {
    /* corrupt or unavailable storage falls back to defaults */
  }
  return {};
}

export function saveRecPrefs(expId: string, prefs: RecPrefs) {
  try {
    localStorage.setItem(REC_PREFS_KEY_PREFIX + expId, JSON.stringify(prefs));
  } catch {
    /* storage unavailable — selection simply won't survive a reload */
  }
}

export function traceLookbackFromParam(value: string | null): number {
  const hours = Number(value);
  return TRACE_LOOKBACK_OPTIONS.includes(
    hours as (typeof TRACE_LOOKBACK_OPTIONS)[number],
  )
    ? hours
    : DEFAULT_TRACE_LOOKBACK_HOURS;
}

export interface ABMetric {
  label: string;
  // +1 = higher mean wins, -1 = lower mean wins. Absent on verdicts stored
  // before the backend started annotating it — evaluatorPolarity() covers those.
  polarity?: number;
  control: { mean: number | null; sampleSize: number | null };
  variants: { name: string; mean: number | null; sampleSize: number | null;
    pValue?: number | null; percentChange?: number | null; isSignificant?: boolean }[];
}

export interface ExperimentInfo {
  id: string;
  name: string;
  agent_id: string;
  agent_name: string;
  status: string;
  stage: string;
  stages: string[];
  running_action: string | null;
  progress: string | null;
  error: string | null;
  created_at: string | null;
  artifacts: {
    agent_meta?: { system_prompt?: string; name?: string;
      tools?: Record<string, string>;
      experiment_capability?: {
        eligible: boolean;
        system_prompt: boolean;
        tool_descriptions: boolean;
        reason: string | null;
      } };
    recommend?: {
      // each generator writes only its own keys — either side may be absent
      recommended_prompt?: string;
      explanation?: string;
      system_prompt_status?: string;
      system_prompt_error?: string;
      tool_status?: string;
      tool_error?: string;
      analyzed_tools?: Record<string, string>;
      tool_descriptions?: Record<string, string>;
      /** 3rd-party tool-description component: attribution + the provider's
       *  change notes (absent on the AgentCore path). */
      tool_provider?: string;
      tool_provider_model_id?: string;
      tool_provider_meta?: { evidence_sessions?: number; tool_calls_seen?: number;
        sessions_with_tool_calls?: number; tool_descriptions_proposed?: number;
        tool_changes?: string[] };
      tool_explanation?: string;
      accepted_prompt?: string;
      accepted_tool_descriptions?: Record<string, string>;
      /** Set by accept: the accepted text differs from the provider's seed. */
      accepted_edited?: boolean;
      /** Who generated the system prompt. Absent ⇒ the AgentCore recommendation
       *  job (its artifact is unchanged); a 3rd-party provider attributes itself. */
      provider?: string;
      provider_model_id?: string;
      provider_meta?: { evidence_sessions?: number; evidence_records?: number;
        sessions_without_transcript?: number; latency_ms?: number;
        input_tokens?: number; output_tokens?: number; calls?: number;
        changes?: string[] };
      /** Which traces this recommendation read — recorded for both paths so a run
       *  stays explainable after the fact. Absent on pre-feature rows. */
      trace_source?: {
        kind: "cloudwatch" | "batch_evaluation";
        lookback_days?: number;
        run_id?: string;
        batch_eval_id?: string;
        batch_evaluation_arn?: string;
        run_mode?: string;
        session_count?: number;
      };
    };
    bundles?: {
      control: { bundle_id?: string; arn: string; version?: string };
      treatment: { bundle_id?: string; arn: string; version?: string };
    };
    gateway?: { gateway_id: string; gateway_url?: string; target_v1?: string;
      online_evaluators?: string[] };
    abtest?: { ab_test_id: string };
    // status_counts is diagnostic only (throttling shows up as a "429" bucket);
    // absent on artifacts written before the concurrent send landed
    traffic?: { sent: number; failed: number; dataset_id?: string;
      dataset_name?: string; status_counts?: Record<string, number> };
    verdict?: { verdict: string; avg_delta?: number; n?: number;
      significant?: boolean; metrics: ABMetric[] };
    promotion_attempt?: {
      ab_test_id: string;
      ab_test_status: string;
      stopped_at: string;
      deployment_id?: string;
      job_id?: string;
    };
    promote?: {
      after_weights?: Record<string, number>;
      prior_shift?: Record<string, number>;
      ab_test_id?: string;
      ab_test_status?: string;
      agent_id?: string;
      deployment_id?: string;
      job_id?: string;
      agent_version?: string | null;
      applied_system_prompt?: boolean;
      applied_tool_descriptions?: string[];
      completed_at?: string;
    };
    canary?: {
      canary_ab_test_id: string;
      weights?: Record<string, number>;
      after_weights?: Record<string, number>;
      ramp_stage: number;
      challenger_agent?: string;
    };
    cleanup?: { category: string; status: string }[];
  };
}


// What the online evaluation config scores both arms with when the operator
// doesn't touch the chips — mirrors service.ONLINE_EVAL_DEFAULT.
export const ONLINE_EVAL_DEFAULT = ["Builtin.GoalSuccessRate", "Builtin.Helpfulness"];
export const ONLINE_EVAL_MAX = 10;  // CreateOnlineEvaluationConfig caps the list at 10

// Mirrors backend STAGES (app/optimization/models.py) — the sidebar renders
// the loop even before any experiment exists, so the list is static here.
export const LOOP_STAGES = [
  "recommend", "bundles", "gateway", "abtest", "traffic", "verdict",
  "promote", "cleanup",
];

// "0.0310" reads worse than "0.031"; tiny values collapse to a bound.
export function fmtP(p: number): string {
  return p < 0.001 ? "<0.001" : p.toFixed(3);
}

// A non-significant "winner" is noise — the label stays neutral wherever a
// verdict is displayed (detail headline, list rows, terminal summary).
export function verdictLabel(
  t: TFunction,
  v: ExperimentInfo["artifacts"]["verdict"] | undefined,
): string {
  if (!v) return "—";
  if (v.significant === false) return t("evalPage.experiment.nonsig.title");
  return v.verdict.toUpperCase();
}

import type { TFunction } from "i18next";
import type { CSSProperties } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ExternalLink, FlaskConical, Gauge, RefreshCw } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import {
  Btn,
  Chip,
  ConfirmDialog,
  DiffPanes,
  Pager,
  Panel,
  StageCard,
  useTablePage,
  useToast,
  ViewHead,
} from "../components";
import { EvaluationNav } from "../components/EvaluationNav";
import type { AgentInfo } from "../lib/api";
import { api } from "../lib/api";
import {
  ACTIVE_RUN_STATUSES,
  DEFAULT_EVALUATORS,
  type EvaluationDatasetInfo as DatasetInfo,
  type EvaluationRunInfo,
  type ExperimentReadiness,
} from "../lib/evaluation";
import { evaluatorLabel, evaluatorPolarity } from "../lib/evaluators";
import { fmtScore } from "../lib/format";
import { RuntimeCanaryView } from "./EvaluationRuntimeCanary";

const DEFAULT_TRACE_LOOKBACK_HOURS = 24;
const TRACE_LOOKBACK_OPTIONS = [24, 72, 168, 720] as const;

// The RECOMMEND generator checkboxes exist only before the stage runs, so the
// backend has nothing to restore them from — persist per experiment locally.
const REC_TYPES_KEY_PREFIX = "launchpad.exp-rec-types.";

function loadRecTypes(expId: string): { sp: boolean; td: boolean } {
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

function saveRecTypes(expId: string, sp: boolean, td: boolean) {
  try {
    localStorage.setItem(REC_TYPES_KEY_PREFIX + expId, JSON.stringify({ sp, td }));
  } catch {
    /* storage unavailable — selection simply won't survive a reload */
  }
}

function traceLookbackFromParam(value: string | null): number {
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
      accepted_prompt?: string;
      accepted_tool_descriptions?: Record<string, string>;
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

interface EvaluatorInfo {
  id: string;
  name?: string;
  level?: string;
  source: string;
  requires_ground_truth?: boolean;
  evaluator_type?: string | null;
  provider?: string | null;
}

// What the online evaluation config scores both arms with when the operator
// doesn't touch the chips — mirrors service.ONLINE_EVAL_DEFAULT.
const ONLINE_EVAL_DEFAULT = ["Builtin.GoalSuccessRate", "Builtin.Helpfulness"];
const ONLINE_EVAL_MAX = 10;  // CreateOnlineEvaluationConfig caps the list at 10

// Mirrors backend STAGES (app/optimization/models.py) — the sidebar renders
// the loop even before any experiment exists, so the list is static here.
const LOOP_STAGES = [
  "recommend", "bundles", "gateway", "abtest", "traffic", "verdict",
  "promote", "cleanup",
];

// "0.0310" reads worse than "0.031"; tiny values collapse to a bound.
function fmtP(p: number): string {
  return p < 0.001 ? "<0.001" : p.toFixed(3);
}

// A non-significant "winner" is noise — the label stays neutral wherever a
// verdict is displayed (detail headline, list rows, terminal summary).
function verdictLabel(
  t: TFunction,
  v: ExperimentInfo["artifacts"]["verdict"] | undefined,
): string {
  if (!v) return "—";
  if (v.significant === false) return t("evalPage.experiment.nonsig.title");
  return v.verdict.toUpperCase();
}

// status → chip tone, shared by the sub-page header and the dashboard row.
function experimentTone(status: string): "good" | "warn" | "crit" | "muted" {
  if (status === "failed") return "crit";
  if (status === "cleaned") return "muted";
  if (status === "running") return "warn";
  return "good"; // ready | promoted
}

export function ExperimentView({ onBack }: { onBack: () => void }) {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const canaryMode = searchParams.get("mode") === "canary";
  const creatingExperiment = !canaryMode && searchParams.get("exp") === "new";
  const backToExperimentList = () => {
    setSearchParams({ view: "experiment" }, { replace: true });
  };
  const switchMode = (mode: "configuration" | "canary") => {
    if (mode === "canary") {
      const selectedCanary = searchParams.get("canary");
      setSearchParams({
        view: "experiment",
        mode: "canary",
        ...(selectedCanary ? { canary: selectedCanary } : {}),
      });
      return;
    }
    const selectedExperiment = searchParams.get("exp");
    setSearchParams({
      view: "experiment",
      ...(selectedExperiment ? { exp: selectedExperiment } : {}),
    });
  };

  return (
    <section>
      <ViewHead
        kicker={t("evaluation.kicker")}
        title={t(
          creatingExperiment ? "expPage.start" : "evalPage.experiment.title",
        )}
        meta={t(
          creatingExperiment
            ? "expPage.startHint"
            : canaryMode
              ? "canaryPage.meta"
              : "evalPage.experiment.meta",
        )}
      />
      <EvaluationNav />
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12,
                    alignItems: "center", flexWrap: "wrap", marginBottom: 14 }}>
        <Btn onClick={creatingExperiment ? backToExperimentList : onBack}>
          ◂ {t(
            creatingExperiment
              ? "evalPage.experiment.title"
              : "evalPage.backToRuns",
          )}
        </Btn>
        {!creatingExperiment && (
          <div
            role="tablist"
            aria-label={t("canaryPage.modeLabel")}
            style={{
              display: "grid",
              gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
              width: "min(100%, 430px)",
              border: "1px solid var(--line)",
              borderRadius: 4,
              padding: 3,
              gap: 3,
            }}
          >
            <Btn
              role="tab"
              aria-selected={!canaryMode}
              data-testid="mode-configuration"
              onClick={() => switchMode("configuration")}
              style={{
                minWidth: 0,
                minHeight: 34,
                borderColor: !canaryMode ? "var(--warn)" : "transparent",
                color: !canaryMode ? "var(--warn)" : undefined,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                gap: 6,
                whiteSpace: "normal",
              }}
            >
              <FlaskConical size={14} />
              {t("canaryPage.mode.configuration")}
            </Btn>
            <Btn
              role="tab"
              aria-selected={canaryMode}
              data-testid="mode-canary"
              onClick={() => switchMode("canary")}
              style={{
                minWidth: 0,
                minHeight: 34,
                borderColor: canaryMode ? "var(--warn)" : "transparent",
                color: canaryMode ? "var(--warn)" : undefined,
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                gap: 6,
                whiteSpace: "normal",
              }}
            >
              <Gauge size={14} />
              {t("canaryPage.mode.canary")}
            </Btn>
          </div>
        )}
      </div>
      {canaryMode ? <RuntimeCanaryView /> : <ConfigurationExperimentView />}
    </section>
  );
}

function ConfigurationExperimentView() {
  const { t } = useTranslation();
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const expParam = searchParams.get("exp");
  const requestedAgentId = searchParams.get("agent");
  const requestedLookbackHours = traceLookbackFromParam(searchParams.get("lookback"));
  const baselineRunId = searchParams.get("baselineRun");
  const sourceRunId = searchParams.get("sourceRun");
  const creatingNew = expParam === "new";
  const [experiments, setExperiments] = useState<ExperimentInfo[]>([]);
  const [activeAgents, setActiveAgents] = useState<AgentInfo[]>([]);
  const [datasets, setDatasets] = useState<DatasetInfo[]>([]);
  const [evaluators, setEvaluators] = useState<EvaluatorInfo[]>([]);
  const [onlineEvaluators, setOnlineEvaluators] = useState<string[]>(ONLINE_EVAL_DEFAULT);
  const [busy, setBusy] = useState(false);
  const [startAgentId, setStartAgentId] = useState("");
  const [startError, setStartError] = useState<string | null>(null);
  const [readiness, setReadiness] = useState<ExperimentReadiness | null>(null);
  const [readinessLoading, setReadinessLoading] = useState(false);
  const [readinessLookbackHours, setReadinessLookbackHours] = useState(
    requestedLookbackHours,
  );
  const [sparseAcknowledged, setSparseAcknowledged] = useState(false);
  const [unavailableAcknowledged, setUnavailableAcknowledged] = useState(false);
  const [baselineDatasetId, setBaselineDatasetId] = useState("");
  const [baselineBusy, setBaselineBusy] = useState(false);
  const [trackedRun, setTrackedRun] = useState<EvaluationRunInfo | null>(null);
  const [trackedRunError, setTrackedRunError] = useState<string | null>(null);
  const readinessRequest = useRef(0);
  const [trafficDatasetId, setTrafficDatasetId] = useState("");
  const [editedPrompt, setEditedPrompt] = useState<string | null>(null);
  const [editedToolJson, setEditedToolJson] = useState<string | null>(null);
  // recommend generators are separately selectable — prompt & tool
  // descriptions come from two different AgentCore recommendation jobs
  const [genSp, setGenSp] = useState(false);
  const [genTd, setGenTd] = useState(false);
  // RECOMMEND trace source: "" = the default rolling window; otherwise the id of one
  // of this agent's completed runs, which pins the input to exactly the sessions
  // that run analysed (an Insights job being the point of the feature).
  const [recSourceRunId, setRecSourceRunId] = useState("");
  const [recSourceRuns, setRecSourceRuns] = useState<EvaluationRunInfo[]>([]);
  const [toolInputsJson, setToolInputsJson] = useState<string | null>(null);
  const [confirmCleanup, setConfirmCleanup] = useState(false);
  const [confirmPromote, setConfirmPromote] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/experiments");
      if (res.ok) {
        setExperiments(((await res.json()) as { experiments: ExperimentInfo[] }).experiments);
      }
    } catch {
      /* backend offline */
    }
  }, []);

  const loadReadiness = useCallback(async (force = false) => {
    if (!startAgentId) {
      setReadiness(null);
      return;
    }
    const request = ++readinessRequest.current;
    setReadinessLoading(true);
    try {
      const params = new URLSearchParams({
        agent_id: startAgentId,
        lookback_hours: String(readinessLookbackHours),
        ...(force ? { force: "true" } : {}),
      });
      const res = await fetch(`/api/experiments/readiness?${params}`);
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { message?: string };
        throw new Error(body.message ?? `HTTP ${res.status}`);
      }
      const body = (await res.json()) as ExperimentReadiness;
      if (request === readinessRequest.current) setReadiness(body);
    } catch {
      if (request === readinessRequest.current) {
        setReadiness({
          agent_id: startAgentId,
          lookback_hours: readinessLookbackHours,
          state: "unavailable",
          trace_count: 0,
          session_count: 0,
          latest_trace_at: null,
          observed_tools: [],
          expected_tools: [],
          missing_tools: [],
          latest_run: null,
          message: null,
        });
      }
    } finally {
      if (request === readinessRequest.current) setReadinessLoading(false);
    }
  }, [readinessLookbackHours, startAgentId]);

  useEffect(() => {
    setReadinessLookbackHours(requestedLookbackHours);
  }, [requestedLookbackHours]);

  useEffect(() => {
    api
      .listAgents()
      .then((res) => {
        const active = res.agents.filter((a) => a.status === "active");
        const eligible = active.filter((a) => a.experiment_capability.eligible);
        setActiveAgents(active);
        setStartAgentId((previous) => {
          const requested = eligible.find((agent) => agent.id === requestedAgentId);
          if (requested) return requested.id;
          if (eligible.some((agent) => agent.id === previous)) return previous;
          return eligible[0]?.id ?? "";
        });
      })
      .catch(() => {});
    // simulated datasets need an actor loop — the traffic stage only replays
    // plain prompt sets (legacy / predefined)
    fetch("/api/eval/datasets")
      .then((res) => (res.ok ? res.json() : { datasets: [] }))
      .then((body: { datasets: DatasetInfo[] }) => {
        const runnable = body.datasets.filter((d) => d.kind !== "simulated");
        setDatasets(runnable);
        setBaselineDatasetId((previous) =>
          runnable.some((dataset) => dataset.id === previous)
            ? previous
            : (runnable[0]?.id ?? ""));
        setTrafficDatasetId((previous) =>
          runnable.some((dataset) => dataset.id === previous)
            ? previous
            : (runnable[0]?.id ?? ""));
      })
      .catch(() => {});
    // online evaluation scores live traces, so ground-truth-only matchers
    // (Builtin.Trajectory*Match) can never apply here — the backend rejects them
    fetch("/api/eval/evaluators")
      .then((res) => (res.ok ? res.json() : { evaluators: [] }))
      .then((body: { evaluators: EvaluatorInfo[] }) =>
        setEvaluators(body.evaluators.filter((e) => !e.requires_ground_truth)))
      .catch(() => {});
    void refresh();
    const timer = setInterval(() => void refresh(), 8000);
    return () => clearInterval(timer);
  }, [refresh, requestedAgentId]);

  useEffect(() => {
    setSparseAcknowledged(false);
    setUnavailableAcknowledged(false);
    setStartError(null);
    void loadReadiness();
  }, [loadReadiness]);

  const trackedRunId = baselineRunId ?? sourceRunId;
  useEffect(() => {
    if (!trackedRunId) {
      setTrackedRun(null);
      setTrackedRunError(null);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let priorStatus: string | null = null;
    const poll = async () => {
      try {
        const res = await fetch(`/api/eval/runs/${trackedRunId}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const run = (await res.json()) as EvaluationRunInfo;
        if (cancelled) return;
        setTrackedRun(run);
        setTrackedRunError(null);
        if (baselineRunId && run.status !== priorStatus) {
          priorStatus = run.status;
          void loadReadiness(true);
        }
        if (baselineRunId && ACTIVE_RUN_STATUSES.has(run.status)) {
          timer = setTimeout(() => void poll(), 2500);
        }
      } catch (error) {
        if (!cancelled) {
          setTrackedRunError(String(error));
          if (baselineRunId) {
            timer = setTimeout(() => void poll(), 2500);
          }
        }
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [baselineRunId, loadReadiness, trackedRunId]);

  const agents = activeAgents.filter((a) => a.experiment_capability.eligible);
  const unsupportedAgents = activeAgents.filter(
    (a) => !a.experiment_capability.eligible,
  );

  // "?exp=<id>" selects a row from the list (linkable, back-button friendly);
  // "?exp=new" opens the start form even while other experiments exist.
  const exp = creatingNew
    ? null
    : (experiments.find((e) => e.id === expParam) ?? experiments[0] ?? null);
  const selectExp = (id: string | null) => {
    setSearchParams(
      id
        ? {
            view: "experiment",
            exp: id,
            ...(id === "new" && startAgentId ? { agent: startAgentId } : {}),
            ...(id === "new" && readinessLookbackHours !== DEFAULT_TRACE_LOOKBACK_HOURS
              ? { lookback: String(readinessLookbackHours) }
              : {}),
          }
        : { view: "experiment" },
    );
  };
  const selectStartAgent = (agentId: string) => {
    readinessRequest.current += 1;
    setReadiness(null);
    setReadinessLoading(!!agentId);
    setStartAgentId(agentId);
    if (creatingNew) {
      setSearchParams({
        view: "experiment",
        exp: "new",
        agent: agentId,
        ...(readinessLookbackHours !== DEFAULT_TRACE_LOOKBACK_HOURS
          ? { lookback: String(readinessLookbackHours) }
          : {}),
      });
    }
  };
  const selectReadinessLookback = (hours: number) => {
    readinessRequest.current += 1;
    setReadiness(null);
    setReadinessLoading(!!startAgentId);
    setReadinessLookbackHours(hours);
    if (creatingNew) {
      const next = new URLSearchParams(searchParams);
      if (hours === DEFAULT_TRACE_LOOKBACK_HOURS) next.delete("lookback");
      else next.set("lookback", String(hours));
      setSearchParams(next);
    }
  };
  // per-experiment control state must not leak across row switches; the
  // generator checkboxes are restored from their per-experiment stash instead
  // of being reset, so re-opening an in-progress experiment keeps them
  useEffect(() => {
    setTrafficDatasetId("");
    setEditedPrompt(null);
    setEditedToolJson(null);
    const recTypes = exp?.id ? loadRecTypes(exp.id) : { sp: false, td: false };
    setGenSp(recTypes.sp);
    setGenTd(recTypes.td);
    setToolInputsJson(null);
    setConfirmCleanup(false);
    setConfirmPromote(false);
  }, [exp?.id]);

  // an action is running server-side — poll fast so its progress line moves
  const runningAction = exp?.running_action ?? null;
  useEffect(() => {
    if (!runningAction) return;
    const timer = setInterval(() => void refresh(), 2500);
    return () => clearInterval(timer);
  }, [runningAction, refresh]);

  const a = exp?.artifacts ?? {};
  const verdict = a.verdict;
  const canary = a.canary;
  const promotion = a.promote;
  const promotionComplete = !!(
    promotion?.deployment_id && promotion.ab_test_status === "STOPPED"
  );
  const legacyPromotion = !!(promotion?.after_weights && !promotionComplete);
  const promotionRunning = exp?.running_action === "promote";
  const promotionFailed = !promotionRunning
    && !!exp?.error?.startsWith("promote: ");
  const toolDescriptionsSupported =
    a.agent_meta?.experiment_capability?.tool_descriptions ?? true;
  useEffect(() => {
    if (!toolDescriptionsSupported) setGenTd(false);
  }, [exp?.id, toolDescriptionsSupported]);
  const canaryWeights = canary?.after_weights ?? canary?.weights;
  const insufficient = !!verdict?.verdict.includes("insufficient");
  // significant:false means the service compared the arms and the delta is
  // within noise — announcing a winner would be misleading, so the verdict
  // headline turns neutral and PROMOTE demands an explicit override.
  const nonSignificant = verdict?.significant === false;
  const verdictHeadline = verdictLabel(t, verdict);
  // cleaned/failed experiments are over — controls that would fire actions
  // against torn-down resources collapse into a read-only summary.
  const terminal = exp?.status === "cleaned" || exp?.status === "failed";
  // one active A/B test per shared gateway — the backend rejects a second
  // concurrent loop (409 experiment.already_running), so gate START up front.
  const hasRunning = experiments.some((e) => e.status === "running");
  const { rows: pageExperiments, pagerProps } = useTablePage(
    experiments,
    experiments.findIndex((e) => e.id === exp?.id),
  );
  const readinessMatchesAgent = readiness?.agent_id === startAgentId;
  const readinessAllowsCreate = readinessMatchesAgent && (
    readiness.state === "ready"
    || (readiness.state === "sparse" && sparseAcknowledged)
    || (readiness.state === "unavailable" && unavailableAcknowledged)
  );

  // old auto-pipeline rows never wrote accepted_* — their bundles artifact
  // marks the recommend card done
  const acceptedPrompt = a.recommend?.accepted_prompt;
  const recommendDone = !!(acceptedPrompt || a.bundles);
  const activeCard = !recommendDone ? "recommend"
    : !a.bundles ? "bundles"
      : !a.gateway || !a.abtest ? "gwab"
        : !a.traffic ? "traffic"
          : !a.verdict ? "verdict"
            : "post";

  const onStart = async () => {
    setStartError(null);
    setBusy(true);
    try {
      const res = await fetch("/api/experiments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: startAgentId,
          lookback_hours: readinessLookbackHours,
        }),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { message?: string };
        setStartError(body.message ?? `HTTP ${res.status}`);
        return;
      }
      void refresh();
      if (creatingNew) selectExp(null); // jump to the freshly created (newest) one
    } catch (err) {
      setStartError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const startBaseline = async () => {
    if (!startAgentId || !baselineDatasetId) return;
    setBaselineBusy(true);
    setTrackedRunError(null);
    try {
      const res = await fetch("/api/eval/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: startAgentId,
          dataset_id: baselineDatasetId,
          mode: "evaluators",
          evaluators: DEFAULT_EVALUATORS,
          wait_seconds: 180,
        }),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { message?: string };
        setTrackedRunError(body.message ?? `HTTP ${res.status}`);
        return;
      }
      const run = (await res.json()) as EvaluationRunInfo;
      setTrackedRun(run);
      setSearchParams({
        view: "experiment",
        exp: "new",
        agent: startAgentId,
        ...(readinessLookbackHours !== DEFAULT_TRACE_LOOKBACK_HOURS
          ? { lookback: String(readinessLookbackHours) }
          : {}),
        baselineRun: run.id,
      });
    } catch (error) {
      setTrackedRunError(String(error));
    } finally {
      setBaselineBusy(false);
    }
  };

  const onAction = async (
    expId: string, action: string, extra?: Record<string, unknown>,
  ) => {
    setBusy(true);
    try {
      const res = await fetch(`/api/experiments/${expId}/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, ...extra }),
      });
      if (!res.ok) {
        const env = (await res.json().catch(() => ({}))) as { message?: string };
        toast(t("common.actionFailed", { msg: env.message ?? `HTTP ${res.status}` }));
      } else {
        // 200/202 both echo the row — apply it now so the button flips to
        // its running state before the next poll tick
        const body = (await res.json()) as { experiment: ExperimentInfo };
        setExperiments((prev) =>
          prev.map((e) => (e.id === body.experiment.id ? body.experiment : e)));
      }
      void refresh();
    } catch (err) {
      toast(t("common.actionFailed", { msg: String(err) }));
    } finally {
      setBusy(false);
    }
  };

  // "button while pending → artifact echo once done": while the backend runs
  // this action the button is a disabled spinner with the row's progress line;
  // a stored `<action>: …` error turns it into a retry.
  const actionBtn = (
    action: string, label: string,
    opts: { primary?: boolean; disabled?: boolean;
      extra?: Record<string, unknown> } = {},
  ) => {
    if (!exp) return null;
    const running = exp.running_action === action;
    const failed = !running && !!exp.error?.startsWith(`${action}: `);
    return (
      <div>
        <Btn
          primary={opts.primary && !failed}
          disabled={busy || !!exp.running_action || opts.disabled}
          data-testid={`action-${action}`}
          onClick={() => void onAction(exp.id, action, opts.extra)}
        >
          {running ? `◐ ${t("expPage.running")}`
            : failed ? `↻ ${t("expPage.retry")}` : `▸ ${label}`}
        </Btn>
        {running && (
          <div
            className="mono dim"
            data-testid="progress-line"
            style={{ fontSize: 10, marginTop: 4 }}
          >
            {exp.progress ?? "…"}
          </div>
        )}
        {failed && (
          <div
            className="note"
            style={{ borderColor: "var(--crit)", marginTop: 6 }}
            data-testid={`action-error-${action}`}
          >
            <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
            <span className="mono" style={{ fontSize: 10.5 }}>{exp.error}</span>
          </div>
        )}
      </div>
    );
  };

  const readinessTone = readiness?.state === "ready" ? "good"
    : readiness?.state === "missing" ? "crit"
      : readiness?.state === "sparse" ? "warn" : "muted";

  const startForm = (label: string) => (
    <>
      <div className="field">
        <label>{t("evalPage.newRun.agent")}</label>
        <select
          className="input"
          value={startAgentId}
          data-testid="exp-agent-select"
          onChange={(e) => selectStartAgent(e.target.value)}
        >
          {agents.length === 0 && (
            <option value="">{t("evalPage.newRun.noAgents")}</option>
          )}
          {agents.map((ag) => (
            <option key={ag.id} value={ag.id} style={{ background: "#141816" }}>
              {ag.name} · {ag.method}
            </option>
          ))}
          {unsupportedAgents.map((ag) => (
            <option key={ag.id} value="" disabled style={{ background: "#141816" }}>
              {ag.name} · {ag.method} —{" "}
              {ag.experiment_capability.reason_code
                ? t(`expPage.reason.${ag.experiment_capability.reason_code}`)
                : ag.experiment_capability.reason}
            </option>
          ))}
        </select>
      </div>
      {unsupportedAgents.length > 0 && (
        <div className="note" style={{ marginBottom: 10 }}
             data-testid="unsupported-agent-hint">
          <span className="i">[i]</span>
          <span>{t("expPage.unsupportedHint")}</span>
        </div>
      )}

      <div
        data-testid="trace-readiness"
        style={{
          border: "1px solid var(--line-2)",
          borderLeft: `3px solid ${
            readiness?.state === "ready" ? "var(--good)"
              : readiness?.state === "missing" ? "var(--crit)"
                : "var(--warn)"}`,
          padding: "11px 12px",
          marginBottom: 10,
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", gap: 10,
                      alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
          <div className="mono" style={{ fontSize: 10.5, color: "var(--ink-2)" }}>
            {t("expPage.readiness.title")}
          </div>
          <div style={{ display: "flex", gap: 7, alignItems: "center", flexWrap: "wrap" }}>
            <label
              className="mono"
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                fontSize: 9.5,
                color: "var(--ink-2)",
              }}
            >
              {t("expPage.readiness.window")}
              <select
                className="input"
                value={readinessLookbackHours}
                data-testid="readiness-lookback"
                aria-label={t("expPage.readiness.window")}
                onChange={(event) => selectReadinessLookback(Number(event.target.value))}
                style={{ width: "auto", minWidth: 92, height: 30, padding: "3px 26px 3px 8px" }}
              >
                {TRACE_LOOKBACK_OPTIONS.map((hours) => (
                  <option key={hours} value={hours} style={{ background: "#141816" }}>
                    {t(`expPage.readiness.windowOption.h${hours}`)}
                  </option>
                ))}
              </select>
            </label>
            {readiness && (
              <Chip tone={readinessTone} icon={readiness.state === "ready" ? "●" : "◐"}>
                {t(`expPage.readiness.state.${readiness.state}`)}
              </Chip>
            )}
            <Btn
              disabled={readinessLoading || !startAgentId}
              data-testid="readiness-retry"
              title={t("expPage.readiness.retry")}
              onClick={() => void loadReadiness(true)}
            >
              <RefreshCw size={13} />
              {t("expPage.readiness.retry")}
            </Btn>
          </div>
        </div>

        {readinessLoading && !readiness ? (
          <div className="mono dim" style={{ fontSize: 10 }}>
            {t("expPage.readiness.loading")}
          </div>
        ) : readiness ? (
          <>
            <div className="kv">
              <span className="k mono">{t("expPage.readiness.traces")}</span>
              <span className="v mono">{readiness.trace_count}</span>
            </div>
            <div className="kv">
              <span className="k mono">{t("expPage.readiness.sessions")}</span>
              <span className="v mono">{readiness.session_count}</span>
            </div>
            <div className="kv">
              <span className="k mono">{t("expPage.readiness.latest")}</span>
              <span className="v mono">
                {readiness.latest_trace_at
                  ? new Date(readiness.latest_trace_at).toLocaleString()
                  : "—"}
              </span>
            </div>
            {readiness.latest_run && !trackedRunId && (
              <div className="kv">
                <span className="k mono">{t("expPage.readiness.latestRun")}</span>
                <span className="v mono">
                  run-{readiness.latest_run.id.slice(0, 6)}
                  {" · "}
                  {t(`expPage.readiness.runStatus.${readiness.latest_run.status}`)}
                  {" · "}
                  {t("expPage.readiness.runSessions", {
                    count: readiness.latest_run.session_count,
                  })}
                </span>
              </div>
            )}
            {readiness.expected_tools.length > 0 && (
              <>
                <div className="kv">
                  <span className="k mono">{t("expPage.readiness.observedTools")}</span>
                  <span className="v mono">
                    {readiness.observed_tools.join(", ") || "—"}
                  </span>
                </div>
                <div className="kv">
                  <span className="k mono">{t("expPage.readiness.missingTools")}</span>
                  <span className="v mono">
                    {readiness.missing_tools.join(", ") || "—"}
                  </span>
                </div>
              </>
            )}

            {readiness.state === "missing" && (
              <div className="note" style={{ borderColor: "var(--crit)", marginTop: 10 }}
                   data-testid="readiness-missing">
                <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                <span>{t("expPage.readiness.missingHint")}</span>
              </div>
            )}
            {readiness.state === "sparse" && (
              <label className="note" style={{ marginTop: 10, cursor: "pointer" }}
                     data-testid="readiness-sparse">
                <input
                  type="checkbox"
                  checked={sparseAcknowledged}
                  onChange={(event) => setSparseAcknowledged(event.target.checked)}
                />
                <span>{t("expPage.readiness.sparseAck")}</span>
              </label>
            )}
            {readiness.state === "unavailable" && (
              <label className="note" style={{ marginTop: 10, cursor: "pointer" }}
                     data-testid="readiness-unavailable">
                <input
                  type="checkbox"
                  checked={unavailableAcknowledged}
                  onChange={(event) => setUnavailableAcknowledged(event.target.checked)}
                />
                <span>{t("expPage.readiness.unavailableAck")}</span>
              </label>
            )}
          </>
        ) : null}

        {(sourceRunId || baselineRunId) && (
          <div className="note" style={{ marginTop: 10 }} data-testid="handoff-run">
            <span className="i">[i]</span>
            <span>
              {sourceRunId
                ? t("expPage.readiness.sourceRun", { id: sourceRunId.slice(0, 6) })
                : t("expPage.readiness.baselineRun", {
                    id: baselineRunId?.slice(0, 6) ?? "",
                  })}
              {trackedRun && (
                <>
                  {" · "}
                  <span className="mono">
                    {t(`expPage.readiness.runStatus.${trackedRun.status}`)}
                    {" · "}
                    {t("expPage.readiness.runSessions", {
                      count: trackedRun.session_ids.length,
                    })}
                  </span>
                </>
              )}
            </span>
          </div>
        )}
        {trackedRun?.error && (
          <div className="note" style={{ borderColor: "var(--crit)", marginTop: 8 }}>
            <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
            <span>{trackedRun.error}</span>
          </div>
        )}
        {trackedRunError && (
          <div className="note" style={{ borderColor: "var(--crit)", marginTop: 8 }}>
            <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
            <span>{trackedRunError}</span>
          </div>
        )}

        {readiness?.state === "missing" && (
          <div style={{ marginTop: 12 }}>
            <label>{t("expPage.readiness.baselineDataset")}</label>
            <select
              className="input"
              value={baselineDatasetId}
              data-testid="baseline-dataset"
              onChange={(event) => setBaselineDatasetId(event.target.value)}
            >
              {datasets.length === 0 && (
                <option value="">{t("expPage.readiness.noBaselineDataset")}</option>
              )}
              {datasets.map((dataset) => (
                <option key={dataset.id} value={dataset.id}
                        style={{ background: "#141816" }}>
                  {dataset.name} · {dataset.item_count}
                </option>
              ))}
            </select>
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 8,
                          flexWrap: "wrap", marginTop: 8 }}>
              <Btn
                disabled={baselineBusy || !baselineDatasetId || !!(
                  trackedRun && ACTIVE_RUN_STATUSES.has(trackedRun.status)
                )}
                data-testid="generate-baseline"
                onClick={() => void startBaseline()}
              >
                {baselineBusy ? t("expPage.readiness.startingBaseline")
                  : t("expPage.readiness.generateBaseline")}
              </Btn>
              <Btn
                data-testid="open-full-run"
                onClick={() =>
                  setSearchParams({
                    view: "new",
                    agent: startAgentId,
                    return: "experiment",
                    ...(readinessLookbackHours !== DEFAULT_TRACE_LOOKBACK_HOURS
                      ? { lookback: String(readinessLookbackHours) }
                      : {}),
                  })
                }
              >
                <ExternalLink size={13} />
                {t("expPage.readiness.openFullRun")}
              </Btn>
            </div>
          </div>
        )}
      </div>

      {hasRunning && (
        <div className="note" style={{ marginBottom: 10 }} data-testid="running-guard">
          <span className="i">[i]</span>
          <span>{t("evalPage.experiment.runningGuard")}</span>
        </div>
      )}
      {startError && (
        <div className="note" style={{ borderColor: "var(--crit)", marginBottom: 10 }}>
          <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
          <span>{startError}</span>
        </div>
      )}
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <Btn
          primary
          disabled={
            busy || readinessLoading || !startAgentId || hasRunning
            || !readinessAllowsCreate
          }
          data-testid="exp-start-btn"
          onClick={() => void onStart()}
        >
          ▸ {label}
        </Btn>
      </div>
    </>
  );

  // Completed runs of the experiment's own agent that can pin RECOMMEND. Only runs
  // with a batch_eval_id qualify — a window-scoped run never started one, so there
  // is no batch evaluation to read.
  const recSourceAgentId = exp?.agent_id ?? null;
  useEffect(() => {
    if (!recSourceAgentId) {
      setRecSourceRuns([]);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch(
          `/api/eval/runs?agent_id=${encodeURIComponent(recSourceAgentId)}&limit=200`,
        );
        if (!res.ok) return;
        const body = (await res.json()) as { runs: EvaluationRunInfo[] };
        if (cancelled) return;
        setRecSourceRuns(
          body.runs.filter((r) => r.status === "completed" && !!r.batch_eval_id),
        );
      } catch {
        if (!cancelled) setRecSourceRuns([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [recSourceAgentId]);

  // a source from another experiment's agent must not survive an experiment switch
  useEffect(() => {
    setRecSourceRunId("");
  }, [recSourceAgentId]);

  // ── stage cards ────────────────────────────────────────────────────────────
  const currentPrompt = a.agent_meta?.system_prompt ?? "";
  const rec = a.recommend;
  const recToolDescs = Object.fromEntries(
    Object.entries(rec?.tool_descriptions ?? {}).filter(([k]) => k !== "_error"));
  // each generator ran iff its own keys exist — old rows wrote both at once
  // spDone means AWS returned a recommendation. A failed job writes a status +
  // error and no prompt; rows written before that guard pair a failed status
  // with the old generic fallback text — both must read as failed, not done
  // (mirrors service.system_prompt_rec_failed).
  const spFailed = rec != null
    && (rec.system_prompt_error != null
      || (rec.system_prompt_status != null
        && (rec.system_prompt_status !== "COMPLETED"
          || rec.recommended_prompt == null)));
  const spDone = rec?.recommended_prompt != null && !spFailed;
  const tdRan = rec != null
    && (rec.tool_status != null || rec.tool_descriptions != null);
  const treatmentPrompt = acceptedPrompt ?? rec?.recommended_prompt ?? "";
  // after a failed job the editor is seeded with the CONTROL prompt (never a
  // stale fallback) — accepting it as-is would send a failed recommendation
  // downstream as the treatment, so ACCEPT stays disabled until it is edited
  const acceptPromptValue = editedPrompt
    ?? (spFailed ? currentPrompt : rec?.recommended_prompt ?? currentPrompt);
  const acceptBlocked = spFailed
    && acceptPromptValue.trim() === currentPrompt.trim();

  // toolName → current description handed to the tool-description optimizer;
  // discovery covers spec/code tools only, so the set stays user-editable
  // (gateway/MCP tools exist only at runtime)
  const knownTools = rec?.analyzed_tools && Object.keys(rec.analyzed_tools).length
    ? rec.analyzed_tools : (a.agent_meta?.tools ?? {});
  const toolInputsValue = toolInputsJson
    ?? (Object.keys(knownTools).length ? JSON.stringify(knownTools, null, 2) : "");
  const parseToolJson = (raw: string): Record<string, string> | null => {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        return null;
      }
      return Object.fromEntries(
        Object.entries(parsed as Record<string, unknown>)
          .map(([k, v]) => [k, String(v)]));
    } catch {
      return null;
    }
  };
  const toolInputs = toolInputsValue.trim()
    ? parseToolJson(toolInputsValue) : undefined;
  const toolInputsBad = toolInputsValue.trim() !== "" && toolInputs === null;

  const onGenerate = (types: string[], withTools: boolean) => {
    if (!exp) return;
    const extra: Record<string, unknown> = { recommend_types: types };
    if (withTools && toolInputs) extra.recommend_tools = toolInputs;
    void onAction(exp.id, "recommend", extra);
  };

  const recRunning = exp?.running_action === "recommend";
  const recError = !recRunning && !!exp?.error?.startsWith("recommend: ");

  const onAccept = () => {
    if (!exp || acceptBlocked) return;
    let toolDescs: Record<string, string> | undefined;
    const raw = editedToolJson
      ?? (Object.keys(recToolDescs).length
        ? JSON.stringify(recToolDescs, null, 2) : "");
    if (raw.trim()) {
      const parsed = parseToolJson(raw);
      if (parsed === null) {
        toast(t("expPage.invalidToolJson"));
        return;
      }
      toolDescs = parsed;
    }
    void onAction(exp.id, "accept", {
      accepted_prompt: acceptPromptValue,
      accepted_tool_descriptions: toolDescs,
    });
  };

  // tools-to-analyze editor — shared by the initial generator form and the
  // regenerate path after an empty/failed tool run
  const toolInputsEditor = (
    <>
      <div className="mono dim" style={{ fontSize: 10, margin: "6px 0 4px" }}>
        {t("expPage.toolsToAnalyze")}
      </div>
      <textarea
        className="input"
        rows={4}
        spellCheck={false}
        data-testid="rec-tools-input"
        placeholder={'{"tool_name": "current description"}'}
        value={toolInputsValue}
        onChange={(e) => setToolInputsJson(e.target.value)}
        style={{ width: "100%", fontFamily: "inherit", fontSize: 11 }}
      />
      {Object.keys(knownTools).length === 0 && !toolInputsValue.trim() && (
        <div className="mono dim" style={{ fontSize: 10, marginTop: 2 }}>
          {t("expPage.noDiscoveredTools")}
        </div>
      )}
      {toolInputsBad && (
        <div className="mono" style={{ fontSize: 10, marginTop: 2,
                                       color: "var(--crit)" }}>
          {t("expPage.invalidToolJson")}
        </div>
      )}
    </>
  );

  const selectGenSp = (v: boolean) => {
    setGenSp(v);
    if (exp) saveRecTypes(exp.id, v, genTd);
  };
  const selectGenTd = (v: boolean) => {
    setGenTd(v);
    if (exp) saveRecTypes(exp.id, genSp, v);
  };

  const recTypeCheckbox = (
    label: string, checked: boolean, set: (v: boolean) => void, testid: string,
    disabled = false,
  ) => (
    <label className="mono" style={{ fontSize: 10.5, display: "inline-flex",
                                     alignItems: "center", gap: 5,
                                     cursor: "pointer" }}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        data-testid={testid}
        onChange={(e) => set(e.target.checked)}
      />
      {label}
    </label>
  );

  // a failed system-prompt job is a hard stop: nothing was recommended, so the
  // stage stays open and ACCEPT is blocked until a re-generation succeeds or
  // the operator writes a treatment prompt themselves
  const spStatusNote = spFailed && (
    <div className="note" style={{ borderColor: "var(--crit)", marginTop: 6 }}
         data-testid="sp-status-note">
      <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
      <span className="mono" style={{ fontSize: 10.5 }}>
        {t("expPage.spRecFailed", {
          status: rec?.system_prompt_status ?? "FAILED",
          msg: rec?.system_prompt_error ?? "",
        })}
      </span>
    </div>
  );

  // failure/empty reasons stay visible — a silently missing section reads
  // as "the feature doesn't exist" (the old behavior this replaces)
  const tdStatusNote = tdRan && Object.keys(recToolDescs).length === 0 && (
    <div className="note" style={{ marginTop: 6 }} data-testid="td-status-note">
      <span className="i">[i]</span>
      <span>
        {rec?.tool_status === "no-tools" ? t("expPage.toolRecNoTools")
          : rec?.tool_status === "error"
            ? t("expPage.toolRecFailed", { msg: rec?.tool_error ?? "" })
            : t("expPage.toolRecEmpty")}
      </span>
    </div>
  );

  const recommendCard = exp && (
    <StageCard
      id="recommend" index={1} title={t("expPage.card.recommend")}
      active={activeCard === "recommend"} done={recommendDone}
    >
      {!rec && (
        <>
          <div className="note" style={{ marginBottom: 8 }}>
            <span className="i">[i]</span>
            <span>{t("expPage.recommendHint")}</span>
          </div>
          <div style={{ display: "flex", gap: 18, marginBottom: 8 }}>
            {recTypeCheckbox(t("expPage.recTypePrompt"), genSp, selectGenSp,
                             "rec-type-sp")}
            {recTypeCheckbox(t("expPage.recTypeTools"), genTd, selectGenTd,
                             "rec-type-td", !toolDescriptionsSupported)}
          </div>
          <div className="field" style={{ marginBottom: 8 }}>
            <label htmlFor="rec-source">{t("expPage.recSource")}</label>
            <select
              id="rec-source"
              data-testid="rec-source"
              className="input mono"
              value={recSourceRunId}
              onChange={(e) => setRecSourceRunId(e.target.value)}
            >
              <option value="" style={{ background: "#141816" }}>
                {t("expPage.recSourceWindow")}
              </option>
              {recSourceRuns.map((run) => (
                <option key={run.id} value={run.id} style={{ background: "#141816" }}>
                  {run.mode === "insights"
                    ? t("expPage.recSourceInsights")
                    : t("expPage.recSourceEval")}
                  {" · "}
                  {run.dataset_name ?? run.id}
                  {/* A window-scoped run records no session ids, so several runs of
                      the same shape would otherwise be indistinguishable here — the
                      timestamp is what lets a user pick the analysis they just ran. */}
                  {" · "}
                  {run.created_at
                    ? new Date(run.created_at).toLocaleString()
                    : run.id}
                  {run.session_ids.length > 0
                    ? ` · ${t("expPage.recSourceSessions", { count: run.session_ids.length })}`
                    : ""}
                </option>
              ))}
            </select>
            <div className="note" style={{ marginTop: 6 }}>
              <span className="i">[i]</span>
              <span>
                {recSourceRunId
                  ? t("expPage.recSourcePinnedNote")
                  : t("expPage.recSourceWindowNote")}
              </span>
            </div>
          </div>
          {genTd && toolDescriptionsSupported && (
            <div style={{ marginBottom: 8 }}>{toolInputsEditor}</div>
          )}
          {!toolDescriptionsSupported && (
            <div className="mono dim" style={{ fontSize: 10, marginBottom: 8 }}>
              {t("expPage.toolBundleUnsupported")}
            </div>
          )}
          {actionBtn("recommend", t("expPage.generateRec"), {
            primary: true,
            disabled: (!genSp && !(genTd && toolDescriptionsSupported))
              || (genTd && toolDescriptionsSupported && toolInputsBad),
            extra: {
              recommend_types: [
                ...(genSp ? ["system_prompt"] : []),
                ...(genTd && toolDescriptionsSupported ? ["tool_descriptions"] : []),
              ],
              ...(genTd && toolDescriptionsSupported && toolInputs
                ? { recommend_tools: toolInputs } : {}),
              ...(recSourceRunId ? { recommend_source_run_id: recSourceRunId } : {}),
            },
          })}
        </>
      )}
      {rec && (
        <>
          {/* Which traces produced this recommendation. Shown after the fact so a
              finished experiment is explainable without re-deriving its input. */}
          {rec.trace_source && (
            <div
              className="mono dim"
              data-testid="rec-trace-source"
              style={{ fontSize: 10, marginBottom: 6 }}
            >
              {rec.trace_source.kind === "batch_evaluation"
                ? t("expPage.recSourceUsedPinned", {
                    kind: rec.trace_source.run_mode === "insights"
                      ? t("expPage.recSourceInsights")
                      : t("expPage.recSourceEval"),
                    id: rec.trace_source.batch_eval_id ?? rec.trace_source.run_id ?? "—",
                    count: rec.trace_source.session_count ?? 0,
                  })
                : t("expPage.recSourceUsedWindow", {
                    days: rec.trace_source.lookback_days ?? 7,
                  })}
            </div>
          )}
          {spDone && (
            <>
              <DiffPanes
                before={currentPrompt}
                after={rec.recommended_prompt ?? ""}
                beforeLabel={t("expPage.currentLabel")}
                afterLabel={t("expPage.recommendedLabel")}
              />
              {rec.explanation && (
                <div className="dim" style={{ fontSize: 10.5, marginTop: 6 }}>
                  {rec.explanation}
                </div>
              )}
            </>
          )}
          {spStatusNote}
          {Object.keys(recToolDescs).length > 0 && (
            <div style={{ marginTop: spDone ? 10 : 0 }}>
              <div className="mono dim" style={{ fontSize: 10, marginBottom: 4 }}>
                {t("expPage.toolRecLabel")}
              </div>
              <DiffPanes
                before={JSON.stringify(rec.analyzed_tools ?? {}, null, 2)}
                after={JSON.stringify(recToolDescs, null, 2)}
                beforeLabel={t("expPage.currentLabel")}
                afterLabel={t("expPage.recommendedLabel")}
              />
            </div>
          )}
          {tdStatusNote}
          {!recommendDone && (!spDone || !tdRan
            || Object.keys(recToolDescs).length === 0) && (
            <div style={{ marginTop: 8 }}>
              {!spDone && (
                <Btn
                  disabled={busy || !!exp.running_action}
                  data-testid="action-recommend-sp"
                  onClick={() => onGenerate(["system_prompt"], false)}
                >
                  ▸ {t("expPage.genSp")}
                </Btn>
              )}
              {toolDescriptionsSupported
                && (!tdRan || (tdRan && Object.keys(recToolDescs).length === 0)) && (
                <div style={{ marginTop: !spDone ? 8 : 0 }}>
                  {toolInputsEditor}
                  <div style={{ marginTop: 6 }}>
                    <Btn
                      disabled={busy || !!exp.running_action || toolInputsBad
                        || !toolInputsValue.trim()}
                      data-testid="action-recommend-td"
                      onClick={() => onGenerate(["tool_descriptions"], true)}
                    >
                      ▸ {t("expPage.genTd")}
                    </Btn>
                  </div>
                </div>
              )}
              {recRunning && (
                <div className="mono dim" data-testid="progress-line"
                     style={{ fontSize: 10, marginTop: 4 }}>
                  {exp.progress ?? "…"}
                </div>
              )}
              {recError && (
                <div className="note" style={{ borderColor: "var(--crit)",
                                               marginTop: 6 }}
                     data-testid="action-error-recommend">
                  <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                  <span className="mono" style={{ fontSize: 10.5 }}>
                    {exp.error}
                  </span>
                </div>
              )}
            </div>
          )}
          {!acceptedPrompt && !a.bundles && (
            <div style={{ marginTop: 10 }}>
              <div className="mono dim" style={{ fontSize: 10, marginBottom: 4 }}>
                {t("expPage.editHint")}
              </div>
              <textarea
                className="input"
                rows={5}
                data-testid="accept-prompt-input"
                value={acceptPromptValue}
                onChange={(e) => setEditedPrompt(e.target.value)}
                style={{ width: "100%", fontFamily: "inherit", fontSize: 11 }}
              />
              {Object.keys(recToolDescs).length > 0 && (
                <>
                  <div className="mono dim" style={{ fontSize: 10, margin: "6px 0 4px" }}>
                    {t("expPage.toolDescs")}
                  </div>
                  <textarea
                    className="input"
                    rows={4}
                    spellCheck={false}
                    data-testid="accept-tools-input"
                    value={editedToolJson ?? JSON.stringify(recToolDescs, null, 2)}
                    onChange={(e) => setEditedToolJson(e.target.value)}
                    style={{ width: "100%", fontFamily: "inherit", fontSize: 11 }}
                  />
                </>
              )}
              {acceptBlocked && (
                <div className="mono" style={{ fontSize: 10, marginTop: 4,
                                               color: "var(--crit)" }}
                     data-testid="accept-blocked-hint">
                  {t("expPage.acceptBlockedRecFailed")}
                </div>
              )}
              <div style={{ marginTop: 8 }}>
                <Btn
                  primary
                  disabled={busy || !!exp.running_action || acceptBlocked}
                  data-testid="action-accept"
                  onClick={onAccept}
                >
                  ▸ {t("expPage.accept")}
                </Btn>
              </div>
            </div>
          )}
          {acceptedPrompt && (
            <div className="mono dim" style={{ fontSize: 10, marginTop: 6 }}>
              ✓ {t("expPage.accepted")}
              {acceptedPrompt.trim() !== (rec.recommended_prompt ?? currentPrompt).trim() &&
                " (edited)"}
            </div>
          )}
        </>
      )}
    </StageCard>
  );

  const bundlesCard = exp && recommendDone && (
    <StageCard
      id="bundles" index={2} title={t("expPage.card.bundles")}
      active={activeCard === "bundles"} done={!!a.bundles}
    >
      <DiffPanes
        before={currentPrompt}
        after={treatmentPrompt}
        beforeLabel={t("expPage.controlLabel")}
        afterLabel={t("expPage.treatmentLabel")}
      />
      <div style={{ marginTop: 8 }}>
        {!a.bundles && actionBtn("bundles", t("expPage.createBundles"),
                                 { primary: true })}
        {a.bundles && (
          <div className="mono dim" style={{ fontSize: 10 }}>
            control: {a.bundles.control.bundle_id ?? a.bundles.control.arn} @{" "}
            {a.bundles.control.version ?? "1"}
            <br />
            treatment: {a.bundles.treatment.bundle_id ?? a.bundles.treatment.arn} @{" "}
            {a.bundles.treatment.version ?? "1"}
          </div>
        )}
      </div>
    </StageCard>
  );

  // One chip renderer for both online-evaluator groups (main + third-party) —
  // the ONLINE_EVAL_MAX gate must apply across the whole selection.
  const onlineEvalChip = (e: EvaluatorInfo) => {
    const on = onlineEvaluators.includes(e.id);
    const full = !on && onlineEvaluators.length >= ONLINE_EVAL_MAX;
    return (
      <button
        key={e.id}
        type="button"
        className={`selchip${on ? " on" : ""}`}
        data-testid={`online-eval-${e.id}`}
        disabled={full}
        style={{ opacity: full ? 0.4 : undefined }}
        title={e.source === "custom" ? t("expPage.customEvaluator") : e.id}
        onClick={() =>
          setOnlineEvaluators((prev) =>
            prev.includes(e.id)
              ? prev.filter((x) => x !== e.id)
              : [...prev, e.id])
        }
      >
        {e.source === "custom" ? (e.name ?? e.id) : evaluatorLabel(t, e.id)}
        {e.source === "custom" && (
          <span className="mono" style={{ fontSize: 8.5, marginLeft: 6 }}>
            ◆
          </span>
        )}
        {e.source === "third_party" && e.provider && (
          <span
            className="mono"
            style={{ fontSize: 8.5, marginLeft: 6, letterSpacing: ".08em", opacity: 0.7 }}
          >
            {e.provider}
          </span>
        )}
      </button>
    );
  };

  const gwabCard = exp && !!a.bundles && (
    <StageCard
      id="gwab" index={3} title={t("expPage.card.gwab")}
      active={activeCard === "gwab"} done={!!a.abtest}
    >
      {!a.gateway && (
        <>
          <div className="field">
            <label>{t("expPage.onlineEvaluators")}</label>
            <div className="note" style={{ marginBottom: 6 }}>
              <span className="i">[i]</span>
              <span>{t("expPage.onlineEvaluatorsHint", { max: ONLINE_EVAL_MAX })}</span>
            </div>
            <div style={{ maxHeight: 132, overflowY: "auto" }}>
              <div className="selchips">
                {evaluators.filter((e) => e.source !== "third_party").map(onlineEvalChip)}
              </div>
              {evaluators.some((e) => e.source === "third_party") && (
                <>
                  <div
                    className="mono dim"
                    style={{ fontSize: 9.5, letterSpacing: ".08em", margin: "8px 0 4px" }}
                  >
                    {t("expPage.thirdPartyGroup")}
                  </div>
                  <div className="selchips">
                    {evaluators.filter((e) => e.source === "third_party").map(onlineEvalChip)}
                  </div>
                </>
              )}
            </div>
          </div>
          {actionBtn("gateway", t("expPage.createGateway"), {
            primary: true,
            disabled: onlineEvaluators.length === 0,
            extra: { online_evaluators: onlineEvaluators },
          })}
        </>
      )}
      {a.gateway && (
        <div className="mono dim" style={{ fontSize: 10, marginBottom: 8 }}>
          gw {a.gateway.gateway_id}
          {a.gateway.target_v1 ? ` · target ${a.gateway.target_v1}` : ""}
          {a.gateway.online_evaluators?.length
            ? ` · ${a.gateway.online_evaluators
                .map((id) => evaluatorLabel(t, id)).join(" + ")}`
            : ""}
        </div>
      )}
      {a.gateway && !a.abtest &&
        actionBtn("abtest", t("expPage.createAbTest"), { primary: true })}
      {a.abtest && (
        <div className="mono dim" style={{ fontSize: 10 }}>
          ab {a.abtest.ab_test_id}
        </div>
      )}
    </StageCard>
  );

  const trafficCard = exp && !!a.abtest && (
    <StageCard
      id="traffic" index={4} title={t("expPage.card.traffic")}
      active={activeCard === "traffic"} done={!!a.traffic}
    >
      {!a.traffic && (
        <div style={{ display: "flex", gap: 9, alignItems: "flex-start",
                      flexWrap: "wrap" }}>
          <select
            className="input"
            style={{ maxWidth: 260 }}
            value={trafficDatasetId}
            data-testid="traffic-dataset-select"
            onChange={(e) => setTrafficDatasetId(e.target.value)}
          >
            {datasets.length === 0 && (
              <option value="">{t("expPage.noTrafficDataset")}</option>
            )}
            {datasets.map((d) => (
              <option key={d.id} value={d.id} style={{ background: "#141816" }}>
                {d.name} ({d.item_count})
              </option>
            ))}
          </select>
          {actionBtn("traffic", t("expPage.sendTraffic"), {
            primary: true,
            disabled: !trafficDatasetId,
            extra: { dataset_id: trafficDatasetId },
          })}
        </div>
      )}
      {a.traffic && (
        <div className="mono dim" style={{ fontSize: 10 }}>
          sent {a.traffic.sent} · failed {a.traffic.failed}
          {a.traffic.dataset_name
            ? ` · ${t("expPage.datasetTag")} ${a.traffic.dataset_name}`
            : ""}
        </div>
      )}
    </StageCard>
  );

  const verdictCard = exp && !!a.traffic && (
    <StageCard
      id="verdict" index={5} title={t("expPage.card.verdict")}
      active={activeCard === "verdict"} done={!!verdict}
    >
      {!verdict && (
        <>
          <div className="note" style={{ marginBottom: 8 }}>
            <span className="i">[i]</span>
            <span>{t("expPage.aggregationHint")}</span>
          </div>
          {actionBtn("verdict", t("expPage.monitorResults"), { primary: true })}
        </>
      )}

      {verdict?.metrics?.length ? (
        verdict.metrics.map((metric) => {
          const variant = metric.variants[0];
          const delta =
            metric.control.mean != null && variant?.mean != null
              ? variant.mean - metric.control.mean
              : null;
          // the raw delta is what operators want to read, but its SIGN only means
          // "better" once oriented: dropping Refusal from 0.2 to 0.0 shows as
          // -0.20 and is an improvement, so polarity drives the colour + marker
          const polarity = metric.polarity ?? evaluatorPolarity(metric.label);
          const oriented = delta == null ? null : delta * polarity;
          return (
            <div className="ab-metric" key={metric.label}>
              <div className="am-h">
                <span>
                  {evaluatorLabel(t, metric.label)}
                  {polarity < 0 && (
                    <span
                      className="mono dim"
                      style={{ fontSize: 8.5, marginLeft: 5 }}
                      title={t("expPage.lowerIsBetterHint")}
                    >
                      ↓ {t("expPage.lowerIsBetter")}
                    </span>
                  )}
                </span>
                {delta != null && (
                  <span
                    className="d"
                    data-testid={`ab-delta-${metric.label}`}
                    style={oriented === 0 ? undefined : {
                      color: (oriented ?? 0) > 0 ? "var(--good)" : "var(--warn)",
                    }}
                    title={t(
                      polarity < 0 ? "expPage.deltaLowerBetter" : "expPage.deltaHigherBetter",
                    )}
                  >
                    {delta >= 0 ? "+" : ""}{delta.toFixed(2)}
                  </span>
                )}
              </div>
              <div className="abbar">
                <span className="an">CONTROL</span>
                <div className="track">
                  <div className="fill" style={{
                    width: `${(metric.control.mean ?? 0) * 100}%`,
                    background: "var(--s1)",
                  }} />
                </div>
                <span className="av">{fmtScore(metric.control.mean)}</span>
              </div>
              <div className="abbar">
                <span className="an">TREAT</span>
                <div className="track">
                  <div className="fill" style={{
                    width: `${(variant?.mean ?? 0) * 100}%`,
                    background: "var(--s3)",
                  }} />
                </div>
                <span className="av">{fmtScore(variant?.mean)}</span>
              </div>
              <div
                className="mono dim"
                data-testid="metric-stats"
                style={{ fontSize: 9.5, marginTop: 2 }}
              >
                n {metric.control.sampleSize ?? "—"}/{variant?.sampleSize ?? "—"}
                {" · "}p={variant?.pValue != null ? fmtP(variant.pValue) : "—"}
                {variant?.isSignificant != null && (
                  <span
                    style={{
                      color: variant.isSignificant ? "var(--good)" : undefined,
                    }}
                  >
                    {" · "}
                    {variant.isSignificant
                      ? `✓ ${t("evalPage.experiment.significant")}`
                      : t("evalPage.experiment.notSignificant")}
                  </span>
                )}
              </div>
            </div>
          );
        })
      ) : null}

      {verdict && (
        <>
          <div
            className="verdict"
            style={
              insufficient || nonSignificant
                ? { background: "rgba(250,178,25,.08)",
                    border: "1px solid rgba(250,178,25,.35)" }
                : undefined
            }
          >
            <span
              className="vt"
              style={insufficient || nonSignificant
                ? { color: "var(--warn)" } : undefined}
            >
              ◎ {verdictHeadline}
            </span>
            <span className="vm">
              {verdict.avg_delta != null && (
                <span title={t("expPage.avgDeltaHint")}>Δ {verdict.avg_delta}</span>
              )} · n={verdict.n ?? 0}
              {verdict.significant === true && (
                <span
                  data-testid="verdict-significance"
                  style={{ color: "var(--good)" }}
                >
                  {" · "}✓ {t("evalPage.experiment.significant")}
                </span>
              )}
              {nonSignificant && (
                <span
                  data-testid="verdict-significance"
                  style={{ color: "var(--warn)" }}
                >
                  {" · "}
                  {t("evalPage.experiment.nonsig.observed",
                    { verdict: verdict.verdict })}
                </span>
              )}
            </span>
            {!promotionComplete && !legacyPromotion && !promotionRunning
              && !promotionFailed && (
              // weak evidence (non-significant or no samples at all)
              // demotes PROMOTE to a secondary, confirm-gated action
              insufficient || nonSignificant ? (
                <Btn
                  style={{ marginLeft: "auto" }}
                  disabled={busy || !!exp.running_action}
                  data-testid="promote-btn"
                  onClick={() => setConfirmPromote(true)}
                >
                  {t("expPage.promote")} ▸
                </Btn>
              ) : (
                <Btn
                  primary
                  style={{ marginLeft: "auto" }}
                  disabled={busy || !!exp.running_action}
                  data-testid="promote-btn"
                  onClick={() => void onAction(exp.id, "promote")}
                >
                  {t("expPage.promote")} ▸
                </Btn>
              )
            )}
            {!promotionComplete && !legacyPromotion
              && (promotionRunning
                || (promotionFailed && !(insufficient || nonSignificant)))
              && actionBtn("promote", t("expPage.promote"), {
                primary: !(insufficient || nonSignificant),
              })}
            {!promotionComplete && !legacyPromotion && promotionFailed
              && (insufficient || nonSignificant) && (
                <Btn
                  style={{ marginLeft: "auto" }}
                  disabled={busy || !!exp.running_action}
                  data-testid="promote-retry-btn"
                  onClick={() => setConfirmPromote(true)}
                >
                  ↻ {t("expPage.retry")}
                </Btn>
              )}
            {legacyPromotion && (
              <div style={{ marginLeft: "auto", display: "flex", gap: 8,
                            alignItems: "center", flexWrap: "wrap" }}>
                <Chip tone="warn" icon="!" data-testid="legacy-promotion-chip">
                  {t("expPage.legacyShift")} · T1{" "}
                  {promotion?.after_weights?.T1 ?? 99}%
                </Chip>
                {promotionRunning
                  ? actionBtn("promote", t("expPage.completePromotion"))
                  : (
                    <Btn
                      disabled={busy || !!exp.running_action}
                      data-testid="complete-promotion-btn"
                      onClick={() => setConfirmPromote(true)}
                    >
                      {promotionFailed
                        ? `↻ ${t("expPage.retry")}`
                        : `▸ ${t("expPage.completePromotion")}`}
                    </Btn>
                  )}
              </div>
            )}
            {promotionComplete && (
              <div style={{ marginLeft: "auto", display: "flex", gap: 8,
                            alignItems: "center", flexWrap: "wrap" }}>
                <Chip tone="good" icon="✓">
                  {t("expPage.promoted")} · v{promotion?.agent_version ?? "—"}
                </Chip>
                <Btn
                  data-testid="handoff-runtime-canary"
                  onClick={() => setSearchParams({
                    view: "experiment",
                    mode: "canary",
                    canary: "new",
                    champion: exp.agent_id,
                    sourceExp: exp.id,
                  })}
                  style={{ display: "inline-flex", alignItems: "center", gap: 6 }}
                >
                  <Gauge size={14} />
                  {t("canaryPage.handoff")}
                </Btn>
              </div>
            )}
          </div>
          {legacyPromotion && (
            <div className="note" style={{ borderColor: "var(--warn)", marginTop: 8 }}
                 data-testid="legacy-promotion-note">
              <span className="i" style={{ color: "var(--warn)" }}>[!]</span>
              <span>{t("expPage.legacyShiftHint")}</span>
            </div>
          )}
          {promotionFailed && legacyPromotion && (
            <div className="note" style={{ borderColor: "var(--crit)", marginTop: 6 }}
                 data-testid="action-error-promote">
              <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
              <span className="mono" style={{ fontSize: 10.5 }}>{exp.error}</span>
            </div>
          )}
          {promotionFailed && !legacyPromotion
            && (insufficient || nonSignificant) && (
              <div className="note" style={{ borderColor: "var(--crit)", marginTop: 6 }}
                   data-testid="action-error-promote">
                <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                <span className="mono" style={{ fontSize: 10.5 }}>{exp.error}</span>
              </div>
            )}
          {(insufficient || nonSignificant) && promotionComplete && (
            <div
              className="mono dim"
              data-testid="promoted-context"
              style={{ fontSize: 10.5, margin: "4px 0 8px" }}
            >
              ⚠ {insufficient
                ? t("evalPage.experiment.insufficient.promotedContext")
                : t("evalPage.experiment.nonsig.promotedContext")}
            </div>
          )}
          {insufficient && !promotionComplete && (
            <div
              className="note"
              style={{ borderColor: "var(--warn)", marginTop: 8 }}
              data-testid="insufficient-note"
            >
              <span className="i" style={{ color: "var(--warn)" }}>[!]</span>
              <span>
                {t("evalPage.experiment.insufficient.reason")}
                <br />· {t("evalPage.experiment.insufficient.a1")}
                <br />· {t("evalPage.experiment.insufficient.a2")}
                <br />· {t("evalPage.experiment.insufficient.a3")}
              </span>
            </div>
          )}
          {nonSignificant && !promotionComplete && (
            <div
              className="note"
              style={{ borderColor: "var(--warn)", marginTop: 8 }}
              data-testid="nonsig-note"
            >
              <span className="i" style={{ color: "var(--warn)" }}>[!]</span>
              <span>
                {t("evalPage.experiment.nonsig.reason")}
                <br />· {t("evalPage.experiment.nonsig.a1")}
                <br />· {t("evalPage.experiment.nonsig.a2")}
                <br />· {t("evalPage.experiment.nonsig.a3")}
              </span>
            </div>
          )}
        </>
      )}
    </StageCard>
  );

  const legacyCanaryArtifact = exp && canary && (
    <div
      data-testid="legacy-canary-artifact"
      style={{
        border: "1px solid var(--line)",
        borderLeft: "3px solid var(--warn)",
        borderRadius: 4,
        padding: "10px 12px",
        marginBottom: 10,
      }}
    >
      <div className="mono" style={{ color: "var(--warn)", fontSize: 11,
                                     fontWeight: 700, marginBottom: 7 }}>
        {t("expPage.legacyCanary.title")}
      </div>
      {canaryWeights && (
        <>
          <div className="split">
            <div style={{
              flex: `0 0 ${canaryWeights.C ?? 90}%`,
              background: "var(--s1)",
            }} />
            <div style={{ flex: 1, background: "var(--s3)" }} />
          </div>
          <div className="mono dim" style={{ fontSize: 9.5 }}>
            champion {canaryWeights.C}% · challenger {canaryWeights.T1}% · stage{" "}
            {(canary.ramp_stage ?? 0) + 1}/3
          </div>
        </>
      )}
      <div className="note" style={{ marginTop: 8 }}>
        <span className="i">[i]</span>
        <span>{t("expPage.legacyCanary.body")}</span>
      </div>
    </div>
  );

  const cleanupCard = exp && (
    <StageCard
      id="cleanup" index={6} title={t("expPage.card.cleanup")}
      active={false} done={!!a.cleanup}
    >
      {!a.cleanup && (
        <Btn
          disabled={busy || !!exp.running_action}
          data-testid="cleanup-btn"
          onClick={() => setConfirmCleanup(true)}
        >
          {exp.running_action === "cleanup"
            ? `◐ ${t("expPage.running")}` : t("expPage.cleanup")}
        </Btn>
      )}
      {exp.running_action === "cleanup" && exp.progress && (
        <div className="mono dim" data-testid="progress-line"
             style={{ fontSize: 10, marginTop: 4 }}>
          {exp.progress}
        </div>
      )}
      {a.cleanup && (
        <div className="code" style={{ maxHeight: 120, overflowY: "auto" }}>
          {a.cleanup
            .map((row) => `${row.status.padEnd(8)} ${row.category}`)
            .join("\n")}
        </div>
      )}
    </StageCard>
  );

  return (
    <>
      {!creatingNew && (
        <Panel
          brk
          pad={false}
          title={t("evalPage.experiment.list.title")}
          sub={t("evalPage.experiment.list.sub")}
          end={
            <Btn
              primary
              disabled={hasRunning}
              title={hasRunning ? t("evalPage.experiment.runningGuard") : undefined}
              data-testid="new-experiment-btn"
              onClick={() => selectExp("new")}
            >
              + {t("evalPage.experiment.list.new")}
            </Btn>
          }
          style={{ "--i": 0, marginBottom: 14 } as CSSProperties}
        >
          <table>
            <thead>
              <tr>
                <th>{t("evalPage.experiment.list.name")}</th>
                <th>{t("evalPage.experiment.list.agent")}</th>
                <th>{t("evalPage.experiment.list.stage")}</th>
                <th>{t("evalPage.experiment.list.verdict")}</th>
                <th>{t("evalPage.experiment.list.created")}</th>
                <th>{t("evalPage.experiment.list.status")}</th>
              </tr>
            </thead>
            <tbody>
              {pageExperiments.map((e) => (
                <tr
                  key={e.id}
                  data-testid="experiment-list-row"
                  onClick={() => selectExp(e.id)}
                  style={{
                    cursor: "pointer",
                    background:
                      exp?.id === e.id ? "rgba(255,176,0,.045)" : undefined,
                  }}
                >
                  <td className="pri">{e.name}</td>
                  <td>{e.agent_name}</td>
                  <td className="mono dim">
                    {e.running_action
                      ? `◐ ${e.running_action.toUpperCase()}`
                      : e.stage.toUpperCase()}
                  </td>
                  <td className="mono dim">{verdictLabel(t, e.artifacts.verdict)}</td>
                  <td className="mono dim">
                    {e.created_at ? new Date(e.created_at).toLocaleString() : "—"}
                  </td>
                  <td>
                    <Chip
                      tone={experimentTone(e.status)}
                      icon={e.status === "running" ? "◐" : "●"}
                    >
                      {e.status.toUpperCase()}
                    </Chip>
                  </td>
                </tr>
              ))}
              {experiments.length === 0 && (
                <tr>
                  <td colSpan={6} className="dim mono" style={{ textAlign: "center" }}>
                    {t("evalPage.experiment.list.empty")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <Pager {...pagerProps} always />
        </Panel>
      )}

      <div className="eval-grid">
        <Panel
          brk
          title={exp ? exp.name : t("expPage.start")}
          sub={exp ? t("expPage.sub") : t("expPage.startHint")}
          end={
            exp && (
              <Chip
                tone={experimentTone(exp.status)}
                icon={exp.status === "running" ? "◐" : "●"}
              >
                {exp.stage.toUpperCase()} · {exp.status.toUpperCase()}
              </Chip>
            )
          }
          style={{ "--i": 1 } as CSSProperties}
        >
          {!exp && startForm(t("expPage.start"))}

          {exp && terminal && (
            <>
              <div data-testid="exp-summary-card">
                {exp.status === "failed" && exp.error && (
                  <div
                    className="note"
                    style={{ borderColor: "var(--crit)", marginBottom: 10 }}
                    data-testid="exp-error"
                  >
                    <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                    <span className="mono">{exp.error}</span>
                  </div>
                )}
                <div className="kv">
                  <span className="k mono">{t("evalPage.experiment.summary.agent")}</span>
                  <span className="v">{exp.agent_name}</span>
                </div>
                <div className="kv">
                  <span className="k mono">{t("evalPage.experiment.summary.verdict")}</span>
                  <span className="v mono">
                    {verdictHeadline}
                    {verdict?.significant === true &&
                      ` · ✓ ${t("evalPage.experiment.significant")}`}
                    {promotionComplete
                      && ` · ${t("expPage.promoted")} v${
                        promotion?.agent_version ?? "—"}`}
                    {legacyPromotion
                      && ` · ${t("expPage.legacyShift")} T1 ${
                        promotion?.after_weights?.T1 ?? 99}%`}
                  </span>
                </div>
                <div className="kv">
                  <span className="k mono">{t("evalPage.experiment.summary.created")}</span>
                  <span className="v mono">
                    {exp.created_at ? new Date(exp.created_at).toLocaleString() : "—"}
                  </span>
                </div>
                {promotionComplete && (insufficient || nonSignificant) && (
                  <div
                    className="mono dim"
                    data-testid="promoted-context"
                    style={{ fontSize: 10.5, margin: "4px 0" }}
                  >
                    ⚠ {insufficient
                      ? t("evalPage.experiment.insufficient.promotedContext")
                      : t("evalPage.experiment.nonsig.promotedContext")}
                  </div>
                )}
                <div className="note" style={{ marginTop: 8 }}>
                  <span className="i">[i]</span>
                  <span>
                    {exp.status === "cleaned"
                      ? t("evalPage.experiment.summary.cleaned")
                      : t("evalPage.experiment.summary.failed")}
                  </span>
                </div>
                {exp.status === "failed" && (
                  <div style={{ marginTop: 10 }}>
                    <Btn
                      disabled={busy}
                      data-testid="cleanup-btn"
                      onClick={() => setConfirmCleanup(true)}
                    >
                      {t("expPage.cleanup")}
                    </Btn>
                  </div>
                )}
                {a.cleanup && (
                  <div className="code" style={{ marginTop: 10, maxHeight: 120, overflowY: "auto" }}>
                    {a.cleanup
                      .map((row) => `${row.status.padEnd(8)} ${row.category}`)
                      .join("\n")}
                  </div>
                )}
              </div>
              <div
                data-testid="start-new"
                style={{ marginTop: 14, borderTop: "1px solid var(--line)", paddingTop: 12 }}
              >
                {startForm(t("evalPage.experiment.startNew"))}
              </div>
            </>
          )}

          {exp && !terminal && (
            <>
              <div className="note" style={{ marginBottom: 10 }}>
                <span className="i">[i]</span>
                <span>{t("expPage.stepHint")}</span>
              </div>
              {recommendCard}
              {bundlesCard}
              {gwabCard}
              {trafficCard}
              {verdictCard}
              {legacyCanaryArtifact}
              {cleanupCard}
            </>
          )}
          {exp && (
            <>
              <ConfirmDialog
                open={confirmCleanup}
                title={t("expPage.confirmCleanup.title")}
                body={t("expPage.confirmCleanup.body")}
                confirmLabel={t("expPage.cleanup")}
                onConfirm={() => {
                  setConfirmCleanup(false);
                  void onAction(exp.id, "cleanup");
                }}
                onCancel={() => setConfirmCleanup(false)}
              />
              <ConfirmDialog
                open={confirmPromote}
                title={legacyPromotion
                  ? t("expPage.confirmCompletePromotion.title")
                  : t("evalPage.experiment.nonsig.confirmPromote.title")}
                body={legacyPromotion
                  ? t("expPage.confirmCompletePromotion.body")
                  : t("evalPage.experiment.nonsig.confirmPromote.body")}
                confirmLabel={legacyPromotion
                  ? t("expPage.completePromotion")
                  : t("expPage.promote")}
                onConfirm={() => {
                  setConfirmPromote(false);
                  void onAction(exp.id, "promote");
                }}
                onCancel={() => setConfirmPromote(false)}
              />
            </>
          )}
        </Panel>

        <Panel
          title={t("evalPage.experiment.how.title")}
          sub={t("evalPage.experiment.how.sub")}
          style={{ "--i": 2 } as CSSProperties}
        >
          {LOOP_STAGES.map((stage, i) => (
            <div className="kv" key={stage}>
              <span className="k mono">{String(i + 1).padStart(2, "0")}</span>
              <span className="v" style={{ textAlign: "left", flex: 1, marginLeft: 12 }}>
                {t(`evalPage.experiment.how.${stage}`)}
              </span>
            </div>
          ))}
          <div className="note" style={{ marginTop: 10 }}>
            <span className="i">[i]</span>
            <span>{t("evalPage.experiment.how.note")}</span>
          </div>
        </Panel>
      </div>
    </>
  );
}

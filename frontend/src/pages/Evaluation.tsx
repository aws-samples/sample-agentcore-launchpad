import type { CSSProperties } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  Btn, Chip, ConfirmDialog, PAGE_SIZES, Pager, Panel, useToast, ViewHead,
} from "../components";
import { EvaluationNav } from "../components/EvaluationNav";
import { InsightClusters } from "../components/InsightClusters";
import type { AgentInfo } from "../lib/api";
import { api } from "../lib/api";
import {
  ACTOR_MODELS,
  CLOUD_VALUE_PREFIX,
  DEFAULT_EVALUATORS,
  SIMULATED_SCHEMA,
  type CloudDatasetInfo as CloudDataset,
  type EvaluationDatasetInfo as Dataset,
  type EvaluationRunInfo as RunInfo,
  hasInsightTrees,
} from "../lib/evaluation";
import { evaluatorLabel } from "../lib/evaluators";
import { DatasetsView } from "./EvaluationDatasets";
import { EvaluatorsView } from "./EvaluationEvaluators";
import { ExperimentView } from "./EvaluationExperiment";
import { OnlineView } from "./EvaluationOnline";

const cloudRunnable = (d: CloudDataset) => d.status === "ACTIVE";

// Keep service-documented evaluators visible while a target region is still
// rolling them out. Remove an id after live ListEvaluators verification shows
// it is available in the deployment region.
const TEMPORARILY_UNAVAILABLE_EVALUATORS = new Set(["Builtin.ContextRelevance"]);

interface EvaluatorInfo {
  id: string;
  name?: string | null;
  level: string;
  source: "builtin" | "custom" | "third_party";
  requires_ground_truth?: boolean;
  evaluator_type?: string | null;
  provider?: string | null;
}

const INSIGHT_TYPES = [
  "Builtin.Insight.FailureAnalysis",
  "Builtin.Insight.UserIntent",
  "Builtin.Insight.ExecutionSummary",
];

const INSIGHT_LABEL_KEYS: Record<string, string> = {
  "Builtin.Insight.FailureAnalysis": "failureAnalysis",
  "Builtin.Insight.UserIntent": "userIntent",
  "Builtin.Insight.ExecutionSummary": "executionSummary",
};

const WINDOW_PRESETS = [1, 6, 24, 72, 168];

const LEVEL_BADGE: Record<string, { label: string; color: string }> = {
  SESSION: { label: "SESSION", color: "var(--warn)" },
  TRACE: { label: "TRACE", color: "var(--aqua)" },
  TOOL_CALL: { label: "TOOL", color: "var(--good)" },
};

// Backend scope encodings for the runs table: "window:24h" → "window · 24h",
// "cloud:name" (AWS cloud dataset) → "☁ name", "online:<configId>" (on-demand
// report of an online insights config) → "online · <configId>".
function scopeLabel(run: RunInfo): string {
  if (run.dataset_name?.startsWith("window:")) {
    return `window · ${run.dataset_name.slice("window:".length)}`;
  }
  if (run.dataset_name?.startsWith("cloud:")) {
    return `☁ ${run.dataset_name.slice("cloud:".length)}`;
  }
  if (run.dataset_name?.startsWith("online:")) {
    return `online · ${run.dataset_name.slice("online:".length)}`;
  }
  if (run.mode === "insights") return `insights · ${run.session_ids.length}`;
  return run.dataset_name ?? "—";
}

export function Evaluation() {
  const { t } = useTranslation();
  const toast = useToast();
  // "?view=new|evaluators" renders a sub-page instead of the dashboard —
  // linkable, and the browser back button returns to the runs list.
  const [searchParams, setSearchParams] = useSearchParams();
  const view = searchParams.get("view");
  const creating = view === "new";
  const requestedAgentId = searchParams.get("agent");
  const returnToExperiment = searchParams.get("return") === "experiment";
  const returnLookback = searchParams.get("lookback");
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [cloudDatasets, setCloudDatasets] = useState<CloudDataset[]>([]);
  // cloud dataset ground-truth flags, fetched lazily per selection
  const [cloudGt, setCloudGt] = useState<Record<string, boolean>>({});
  const [evaluators, setEvaluators] = useState<EvaluatorInfo[]>([]);
  const [runs, setRuns] = useState<RunInfo[]>([]);
  const [runTotal, setRunTotal] = useState(0);
  const [runPage, setRunPage] = useState(1);
  // 20/page like the other Evaluation tables (the hook's default); the
  // Observability tabs keep their larger 50 because their pages come from AWS
  const [runSize, setRunSize] = useState<number>(PAGE_SIZES[0]);
  // The runs table is server-paged, but the insights duplicate guards must see
  // insights runs beyond the displayed page — a missed duplicate costs a real
  // AWS analysis. This page-independent list is what they read.
  const [insightsRuns, setInsightsRuns] = useState<RunInfo[]>([]);
  const [selectedRun, setSelectedRun] = useState<RunInfo | null>(null);
  const [queueLocked, setQueueLocked] = useState(false);
  const [queueMax, setQueueMax] = useState(3);
  const [agentId, setAgentId] = useState("");
  const [datasetId, setDatasetId] = useState("");
  const [actorModelId, setActorModelId] = useState(ACTOR_MODELS[0]);
  const [mode, setMode] = useState<"evaluators" | "insights">("evaluators");
  const [scope, setScope] = useState<"dataset" | "window">("dataset");
  const [lookbackHours, setLookbackHours] = useState(24);
  const [chosenEvaluators, setChosenEvaluators] = useState<string[]>(DEFAULT_EVALUATORS);
  const availableChosenEvaluators = chosenEvaluators.filter(
    (id) => !TEMPORARILY_UNAVAILABLE_EVALUATORS.has(id),
  );
  const [chosenInsights, setChosenInsights] = useState<string[]>(INSIGHT_TYPES);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [insightsBusy, setInsightsBusy] = useState(false);
  const [confirmInsights, setConfirmInsights] = useState(false);

  const failedSeen = useRef<Set<string> | null>(null);
  const refresh = useCallback(async () => {
    try {
      const offset = (runPage - 1) * runSize;
      const [runsRes, insightsRes, queueRes] = await Promise.all([
        fetch(`/api/eval/runs?limit=${runSize}&offset=${offset}`),
        fetch("/api/eval/runs?mode=insights&limit=200"),
        fetch("/api/eval/queue"),
      ]);
      if (runsRes.ok) {
        const body = (await runsRes.json()) as { runs: RunInfo[]; total: number };
        const firstLoad = failedSeen.current === null;
        const seen = (failedSeen.current ??= new Set());
        // only runs actually polled are marked seen — a failure on a page the
        // operator is not looking at toasts late, never silently
        body.runs.forEach((run) => {
          if (run.status !== "failed" || seen.has(run.id)) return;
          seen.add(run.id);
          if (!firstLoad) {
            toast(t("evalPage.runFailedToast", { agent: run.agent_name, msg: run.error ?? "" }));
          }
        });
        setRuns(body.runs);
        setRunTotal(body.total);
        setSelectedRun(
          (prev) => body.runs.find((r) => r.id === prev?.id) ?? body.runs[0] ?? null,
        );
      }
      if (insightsRes.ok) {
        setInsightsRuns(((await insightsRes.json()) as { runs: RunInfo[] }).runs);
      }
      if (queueRes.ok) {
        const queue = (await queueRes.json()) as { locked: boolean; max_concurrency: number };
        setQueueLocked(queue.locked);
        setQueueMax(queue.max_concurrency);
      }
    } catch {
      /* backend offline */
    }
  }, [runPage, runSize, t, toast]);

  useEffect(() => {
    api
      .listAgents()
      .then((res) => {
        // all active agents — harness is eval-supported since 07-13 (its managed
        // runtime emits strands-scoped spans under harness_{name}.DEFAULT)
        const eligible = res.agents.filter(
          (a) => a.status === "active" && a.method !== "discovered_runtime",
        );
        setAgents(eligible);
        setAgentId((previous) => {
          const requested = eligible.find((agent) => agent.id === requestedAgentId);
          if (requested) return requested.id;
          if (eligible.some((agent) => agent.id === previous)) return previous;
          return eligible[0]?.id ?? "";
        });
      })
      .catch(() => {});
    fetch("/api/eval/datasets")
      .then((res) => res.json())
      .then((d: { datasets: Dataset[] }) => {
        setDatasets(d.datasets);
        if (d.datasets.length) setDatasetId(d.datasets[0].id);
      })
      .catch(() => {});
    fetch("/api/eval/datasets/cloud")
      .then((res) => (res.ok ? res.json() : { datasets: [] }))
      .then((d: { datasets: CloudDataset[] }) => {
        setCloudDatasets(d.datasets);
        const firstRunnable = d.datasets.find(cloudRunnable);
        if (firstRunnable) {
          // default only when there is no local dataset to default to
          setDatasetId((prev) => prev || CLOUD_VALUE_PREFIX + firstRunnable.datasetId);
        }
      })
      .catch(() => {});
    fetch("/api/eval/evaluators")
      .then((res) => res.json())
      .then((d: { evaluators: EvaluatorInfo[] }) => setEvaluators(d.evaluators))
      .catch(() => {});
    void refresh();
    const timer = setInterval(() => void refresh(), 8000);
    return () => clearInterval(timer);
  // Query-param sub-pages keep this component mounted. Reload the form options
  // when the view changes so datasets created in DatasetsView are immediately
  // available when the operator opens New Run.
  }, [refresh, requestedAgentId, view]);

  // Trajectory matchers score against expected_trajectory ground truth — only
  // dataset runs whose selected dataset carries it can use them. Cloud
  // datasets don't list ground truth up front, so it's fetched per selection.
  const selectedCloudId = datasetId.startsWith(CLOUD_VALUE_PREFIX)
    ? datasetId.slice(CLOUD_VALUE_PREFIX.length)
    : null;
  useEffect(() => {
    if (!selectedCloudId || selectedCloudId in cloudGt) return;
    fetch(`/api/eval/datasets/cloud/${selectedCloudId}`)
      .then((res) => res.json())
      .then((d: { has_ground_truth?: boolean }) =>
        setCloudGt((p) => ({ ...p, [selectedCloudId]: !!d.has_ground_truth })),
      )
      .catch(() => {});
  }, [selectedCloudId, cloudGt]);
  const selectedDataset = datasets.find((d) => d.id === datasetId);
  const trajectoryAllowed =
    scope === "dataset" &&
    (selectedCloudId
      ? !!cloudGt[selectedCloudId]
      : !!selectedDataset?.has_ground_truth);
  // Simulated persona datasets need an actor model (an LLM plays the user).
  const selectedCloud = cloudDatasets.find((d) => d.datasetId === selectedCloudId);
  const simulatedSelected =
    scope === "dataset" &&
    (selectedCloud
      ? selectedCloud.schemaType === SIMULATED_SCHEMA
      : selectedDataset?.kind === "simulated");
  useEffect(() => {
    if (!trajectoryAllowed) {
      setChosenEvaluators((prev) => prev.filter((id) => !id.startsWith("Builtin.Trajectory")));
    }
  }, [trajectoryAllowed]);

  const startRun = async () => {
    setSubmitError(null);
    const base = {
      agent_id: agentId,
      mode,
      // window runs are passive (no invoke) — nothing to wait for
      wait_seconds: scope === "window" ? 0 : 180,
      ...(scope === "window"
        ? { lookback_hours: lookbackHours }
        : selectedCloudId
          ? { cloud_dataset_id: selectedCloudId }
          : { dataset_id: datasetId }),
      ...(simulatedSelected ? { actor_model_id: actorModelId } : {}),
    };
    const payload =
      mode === "insights"
        ? { ...base, insights: chosenInsights }
        : { ...base, evaluators: availableChosenEvaluators };
    const res = await fetch("/api/eval/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const body = (await res.json()) as { message?: string };
      setSubmitError(body.message ?? `http ${res.status}`);
      return;
    }
    const created = (await res.json()) as RunInfo;
    toast(t("evalPage.newRun.submitted"));
    if (returnToExperiment) {
      setSearchParams({
        view: "experiment",
        exp: "new",
        agent: agentId,
        ...(returnLookback ? { lookback: returnLookback } : {}),
        baselineRun: created.id,
      });
    } else {
      setSearchParams({}, { replace: true }); // back to the runs list
    }
    void refresh();
  };

  const selectRunAgent = (nextAgentId: string) => {
    setAgentId(nextAgentId);
    if (returnToExperiment) {
      setSearchParams({
        view: "new",
        agent: nextAgentId,
        return: "experiment",
        ...(returnLookback ? { lookback: returnLookback } : {}),
      });
    }
  };

  // Contextual re-run from the dashboard: insights over the sessions a
  // completed run already produced (no re-invoke, so wait_seconds 0).
  const startInsightsOnRun = async (run: RunInfo) => {
    setInsightsBusy(true);
    try {
      const res = await fetch("/api/eval/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          agent_id: run.agent_id,
          mode: "insights",
          session_ids: run.session_ids,
          wait_seconds: 0,
        }),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { message?: string };
        toast(t("common.actionFailed", { msg: body.message ?? `HTTP ${res.status}` }));
        return;
      }
      toast(t("evalPage.newRun.submitted"));
      void refresh();
    } finally {
      setInsightsBusy(false);
    }
  };

  // An insights run over this exact session set that hasn't settled yet —
  // re-clicking would only enqueue a duplicate behind the account lock.
  const insightsPending = (run: RunInfo): boolean => {
    const key = [...(run.session_ids ?? [])].sort().join(",");
    return insightsRuns.some(
      (r) =>
        r.status !== "completed" &&
        r.status !== "failed" &&
        [...(r.session_ids ?? [])].sort().join(",") === key,
    );
  };

  // A completed insights run over this exact session set already exists —
  // confirming would repeat the whole analysis (new run, new service usage).
  const insightsAlreadyRan = (run: RunInfo): boolean => {
    const key = [...(run.session_ids ?? [])].sort().join(",");
    return insightsRuns.some(
      (r) =>
        r.status === "completed" &&
        [...(r.session_ids ?? [])].sort().join(",") === key,
    );
  };

  const statusChip = (run: RunInfo) => {
    if (run.status === "completed")
      return <Chip tone="good" icon="●">{t("evalPage.status.completed")}</Chip>;
    if (run.status === "failed")
      return <Chip tone="crit" icon="✕">{t("evalPage.status.failed")}</Chip>;
    if (run.status === "queued" && (run.queue_position ?? 0) >= 1)
      return <Chip tone="muted" icon="◌">{t("evalPage.status.queued")}</Chip>;
    return <Chip tone="warn" icon="◐">{run.status.toUpperCase()}</Chip>;
  };

  const average = (run: RunInfo): string => {
    if (!run.scores.length) return "—";
    const mean = run.scores.reduce((acc, s) => acc + s.score, 0) / run.scores.length;
    return mean.toFixed(2);
  };

  // ── Evaluators management sub-page (?view=evaluators) ────────────────────
  if (view === "evaluators") {
    return <EvaluatorsView onBack={() => setSearchParams({}, { replace: true })} />;
  }

  // ── Datasets management sub-page (?view=datasets) ─────────────────────────
  if (view === "datasets") {
    return <DatasetsView onBack={() => setSearchParams({}, { replace: true })} />;
  }

  // ── Online evaluation sub-page (?view=online) ─────────────────────────────
  if (view === "online") {
    return <OnlineView onBack={() => setSearchParams({}, { replace: true })} />;
  }

  // ── Experiment sub-page (?view=experiment) ────────────────────────────────
  if (view === "experiment") {
    return <ExperimentView onBack={() => setSearchParams({}, { replace: true })} />;
  }

  // ── New Run sub-page (?view=new) ──────────────────────────────────────────
  if (creating) {
    return (
      <section>
        <ViewHead
          kicker={t("evaluation.kicker")}
          title={t("evalPage.newRun.title")}
          meta={t("evalPage.newRun.sub")}
        />
        <EvaluationNav />
        <div style={{ marginBottom: 14 }}>
          <Btn
            onClick={() =>
              returnToExperiment
                ? setSearchParams({
                    view: "experiment",
                    exp: "new",
                    ...(agentId ? { agent: agentId } : {}),
                    ...(returnLookback ? { lookback: returnLookback } : {}),
                  })
                : setSearchParams({}, { replace: true })
            }
          >
            ◂ {t(returnToExperiment
              ? "evalPage.backToExperiment"
              : "evalPage.backToRuns")}
          </Btn>
        </div>
        <div className="eval-grid">
          <Panel
            brk
            title={t("evalPage.newRun.title")}
            sub={t("evalPage.newRun.sub")}
            style={{ "--i": 0 } as CSSProperties}
          >
            <div className="field">
              <label>{t("evalPage.newRun.mode")}</label>
              <div className="selchips">
                <button
                  type="button"
                  className={`selchip${mode === "evaluators" ? " on" : ""}`}
                  style={{ cursor: "pointer" }}
                  onClick={() => setMode("evaluators")}
                >
                  {t("evalPage.newRun.modeEvaluators")}
                </button>
                <button
                  type="button"
                  className={`selchip${mode === "insights" ? " on" : ""}`}
                  style={{ cursor: "pointer" }}
                  onClick={() => setMode("insights")}
                >
                  {t("evalPage.newRun.modeInsights")}
                </button>
              </div>
            </div>
            <div className="field">
              <label>{t("evalPage.newRun.agent")}</label>
              <select
                className="input"
                value={agentId}
                data-testid="run-agent-select"
                onChange={(e) => selectRunAgent(e.target.value)}
              >
                {agents.length === 0 && (
                  <option value="">{t("evalPage.newRun.noAgents")}</option>
                )}
                {agents.map((a) => (
                  <option key={a.id} value={a.id} style={{ background: "#141816" }}>
                    {a.name} · {a.method}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label>{t("evalPage.newRun.scope")}</label>
              <div className="selchips">
                <button
                  type="button"
                  className={`selchip${scope === "dataset" ? " on" : ""}`}
                  style={{ cursor: "pointer" }}
                  onClick={() => setScope("dataset")}
                >
                  {t("evalPage.newRun.scopeDataset")}
                </button>
                <button
                  type="button"
                  className={`selchip${scope === "window" ? " on" : ""}`}
                  style={{ cursor: "pointer" }}
                  data-testid="scope-window"
                  onClick={() => setScope("window")}
                >
                  {t("evalPage.newRun.scopeWindow")}
                </button>
              </div>
            </div>
            {scope === "dataset" ? (
              <div className="field">
                <label>{t("evalPage.newRun.dataset")}</label>
                <select
                  className="input"
                  value={datasetId}
                  onChange={(e) => setDatasetId(e.target.value)}
                >
                  <optgroup label={t("evalPage.newRun.localGroup")}>
                    {datasets.map((d) => (
                      <option key={d.id} value={d.id} style={{ background: "#141816" }}>
                        {d.name} · {d.item_count} ({d.locale})
                        {d.has_ground_truth ? " ◆" : ""}
                      </option>
                    ))}
                  </optgroup>
                  {cloudDatasets.length > 0 && (
                    <optgroup label={t("evalPage.newRun.cloudGroup")}>
                      {cloudDatasets.map((d) => (
                        <option
                          key={d.datasetId}
                          value={CLOUD_VALUE_PREFIX + d.datasetId}
                          disabled={!cloudRunnable(d)}
                          style={{ background: "#141816" }}
                        >
                          ☁ {d.name} · {d.exampleCount ?? "?"}
                          {d.schemaType === SIMULATED_SCHEMA
                            ? ` · ${t("evalPage.newRun.cloudSimulated")}`
                            : ""}
                          {d.status !== "ACTIVE" ? ` — ${d.status}` : ""}
                        </option>
                      ))}
                    </optgroup>
                  )}
                </select>
                {selectedCloudId && (
                  <div className="note" style={{ marginTop: 8 }}>
                    <span className="i">[i]</span>
                    <span>{t("evalPage.newRun.cloudHint")}</span>
                  </div>
                )}
                {simulatedSelected && (
                  <div style={{ marginTop: 12 }}>
                    <label>{t("evalPage.newRun.actorModel")}</label>
                    <select
                      className="input"
                      value={actorModelId}
                      data-testid="actor-model"
                      onChange={(e) => setActorModelId(e.target.value)}
                    >
                      {ACTOR_MODELS.map((m) => (
                        <option key={m} value={m} style={{ background: "#141816" }}>
                          {m}
                        </option>
                      ))}
                    </select>
                    <div className="note" style={{ marginTop: 8 }}>
                      <span className="i">[i]</span>
                      <span>{t("evalPage.newRun.actorModelHint")}</span>
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="field">
                <label>{t("evalPage.newRun.window")}</label>
                <div className="selchips" style={{ alignItems: "center" }}>
                  {WINDOW_PRESETS.map((h) => (
                    <button
                      key={h}
                      type="button"
                      className={`selchip${lookbackHours === h ? " on" : ""}`}
                      style={{ cursor: "pointer" }}
                      onClick={() => setLookbackHours(h)}
                    >
                      {h}h
                    </button>
                  ))}
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={336}
                    value={lookbackHours}
                    style={{ width: 92 }}
                    aria-label={t("evalPage.newRun.windowCustom")}
                    onChange={(e) =>
                      setLookbackHours(
                        Math.min(336, Math.max(1, Number(e.target.value) || 1)),
                      )
                    }
                  />
                </div>
                <div className="note" style={{ marginTop: 8 }}>
                  <span className="i">[i]</span>
                  <span>{t("evalPage.newRun.windowHint")}</span>
                </div>
              </div>
            )}
            {mode === "evaluators" ? (
              <div className="field">
                <div
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "baseline",
                  }}
                >
                  <label>{t("evalPage.newRun.evaluators")}</label>
                  <button
                    type="button"
                    className="mono"
                    style={{
                      background: "none",
                      border: "none",
                      color: "var(--aqua)",
                      cursor: "pointer",
                      fontSize: 10,
                      letterSpacing: ".08em",
                      padding: 0,
                    }}
                    onClick={() => setSearchParams({ view: "evaluators" })}
                  >
                    {t("evalPage.newRun.manageEvaluators")} ▸
                  </button>
                </div>
                <div style={{ maxHeight: 168, overflowY: "auto" }}>
                  <div className="selchips">
                    {evaluators.filter((e) => e.source !== "third_party").map((e) => {
                      const unavailable = TEMPORARILY_UNAVAILABLE_EVALUATORS.has(e.id);
                      const gated = unavailable || (e.requires_ground_truth && !trajectoryAllowed);
                      const badge = LEVEL_BADGE[e.level];
                      return (
                        <button
                          key={e.id}
                          type="button"
                          className={`selchip${
                            availableChosenEvaluators.includes(e.id) ? " on" : ""
                          }`}
                          style={{ cursor: gated ? "not-allowed" : "pointer",
                                   opacity: gated ? 0.4 : undefined }}
                          disabled={gated}
                          title={
                            unavailable
                              ? t("evalPage.newRun.temporarilyUnavailableHint")
                              : gated
                              ? t("evalPage.newRun.trajectoryNeedsGt")
                              : e.source === "custom"
                                ? t("evalPage.newRun.customTitle")
                                : e.id
                          }
                          onClick={() =>
                            setChosenEvaluators((prev) =>
                              prev.includes(e.id)
                                ? prev.filter((x) => x !== e.id)
                                : [...prev, e.id],
                            )
                          }
                        >
                          {e.source === "custom" ? (e.name ?? e.id) : evaluatorLabel(t, e.id)}
                          {unavailable && (
                            <span
                              className="mono"
                              style={{ fontSize: 8.5, marginLeft: 6, letterSpacing: ".08em" }}
                            >
                              {t("evalPage.newRun.temporarilyUnavailable")}
                            </span>
                          )}
                          {(e.source === "custom" || e.requires_ground_truth) && badge && (
                            <span
                              className="mono"
                              style={{ fontSize: 8.5, marginLeft: 6, color: badge.color,
                                       letterSpacing: ".08em" }}
                            >
                              {e.source === "custom" ? `◆ ${badge.label}` : badge.label}
                            </span>
                          )}
                        </button>
                      );
                    })}
                  </div>
                  {evaluators.some((e) => e.source === "third_party") && (
                    <>
                      <div
                        className="mono dim"
                        style={{ fontSize: 9.5, letterSpacing: ".08em", margin: "8px 0 4px" }}
                      >
                        {t("evalPage.newRun.thirdPartyGroup")}
                      </div>
                      <div className="selchips">
                        {evaluators.filter((e) => e.source === "third_party").map((e) => (
                          <button
                            key={e.id}
                            type="button"
                            className={`selchip${
                              chosenEvaluators.includes(e.id) ? " on" : ""
                            }`}
                            style={{ cursor: "pointer" }}
                            title={e.id}
                            onClick={() =>
                              setChosenEvaluators((prev) =>
                                prev.includes(e.id)
                                  ? prev.filter((x) => x !== e.id)
                                  : [...prev, e.id],
                              )
                            }
                          >
                            {evaluatorLabel(t, e.id)}
                            {e.provider && (
                              <span
                                className="mono"
                                style={{ fontSize: 8.5, marginLeft: 6,
                                         letterSpacing: ".08em", opacity: 0.7 }}
                              >
                                {e.provider}
                              </span>
                            )}
                          </button>
                        ))}
                      </div>
                    </>
                  )}
                </div>
              </div>
            ) : (
              <>
                <div className="field">
                  <label>{t("evalPage.newRun.insightTypes")}</label>
                  <div className="selchips">
                    {INSIGHT_TYPES.map((id) => (
                      <button
                        key={id}
                        type="button"
                        className={`selchip${chosenInsights.includes(id) ? " on" : ""}`}
                        style={{ cursor: "pointer" }}
                        onClick={() =>
                          setChosenInsights((prev) =>
                            prev.includes(id)
                              ? prev.filter((x) => x !== id)
                              : [...prev, id],
                          )
                        }
                      >
                        {t(`evalPage.newRun.insightType.${INSIGHT_LABEL_KEYS[id]}`)}
                      </button>
                    ))}
                  </div>
                </div>
                <div className="note" style={{ marginBottom: 10 }}>
                  <span className="i">[i]</span>
                  <span>
                    {scope === "window"
                      ? t("evalPage.newRun.insightsWindowHint")
                      : t("evalPage.newRun.insightsHint")}
                  </span>
                </div>
              </>
            )}
            {submitError && (
              <div className="note" style={{ borderColor: "var(--crit)", marginBottom: 10 }}>
                <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                <span>{submitError}</span>
              </div>
            )}
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <Btn
                primary
                data-testid="start-run-submit"
                disabled={
                  !agentId ||
                  (scope === "dataset" && !datasetId) ||
                  (mode === "evaluators" && availableChosenEvaluators.length === 0) ||
                  (mode === "insights" && chosenInsights.length === 0)
                }
                onClick={() => void startRun()}
              >
                ▸ {t("evalPage.newRun.submit")}
              </Btn>
            </div>
          </Panel>

          <Panel
            title={t("evalPage.newRun.how.title")}
            sub={t("evalPage.newRun.how.sub")}
            style={{ "--i": 1 } as CSSProperties}
          >
            {(["s1", "s2", "s3", "s4"] as const).map((step, i) => (
              <div className="kv" key={step}>
                <span className="k mono">{`0${i + 1}`}</span>
                <span className="v" style={{ textAlign: "left", flex: 1, marginLeft: 12 }}>
                  {t(`evalPage.newRun.how.${step}`)}
                </span>
              </div>
            ))}
            <div className="note" style={{ marginTop: 10 }}>
              <span className="i">[i]</span>
              <span>{t("evalPage.newRun.how.note")}</span>
            </div>
          </Panel>
        </div>
      </section>
    );
  }

  // ── Dashboard: runs list + selected-run results ───────────────────────────
  return (
    <section>
      <ViewHead
        kicker={t("evaluation.kicker")}
        title={t("evaluation.title")}
        meta={t("evalPage.metaLive")}
      />
      <EvaluationNav />

      <Panel
        brk
        title={t("evalPage.runs.title")}
        sub={t("evalPage.runs.sub")}
        end={
          <>
            {queueLocked ? (
              <Chip tone="warn" icon="◐">{t("evalPage.acctLock", { max: queueMax })}</Chip>
            ) : (
              <Chip tone="good" icon="●">{t("evalPage.queueIdle")}</Chip>
            )}
            <Btn
              onClick={() => setSearchParams({ view: "datasets" })}
              data-testid="datasets-btn"
            >
              ▤ {t("evalPage.datasets.title")}
            </Btn>
            <Btn
              onClick={() => setSearchParams({ view: "evaluators" })}
              data-testid="evaluators-btn"
            >
              ◆ {t("evalPage.evaluators.title")}
            </Btn>
            <Btn
              onClick={() => setSearchParams({ view: "experiment" })}
              data-testid="experiment-btn"
            >
              ⚗ {t("evalPage.experiment.title")}
            </Btn>
            <Btn
              primary
              onClick={() => setSearchParams({ view: "new" })}
              data-testid="new-run-btn"
            >
              + {t("evalPage.runs.newEvaluation")}
            </Btn>
          </>
        }
        pad={false}
        style={{ "--i": 0, marginBottom: 14 } as CSSProperties}
      >
        <table>
          <thead>
            <tr>
              <th>{t("evalPage.runs.run")}</th>
              <th>{t("evalPage.runs.agent")}</th>
              <th>{t("evalPage.runs.dataset")}</th>
              <th>{t("evalPage.runs.evaluators")}</th>
              <th>{t("evalPage.runs.score")}</th>
              <th>{t("evalPage.runs.created")}</th>
              <th>{t("evalPage.runs.status")}</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr
                key={run.id}
                onClick={() => setSelectedRun(run)}
                style={{
                  cursor: "pointer",
                  background:
                    selectedRun?.id === run.id ? "rgba(255,176,0,.045)" : undefined,
                }}
              >
                <td className="mono">run-{run.id.slice(0, 6)}</td>
                <td className="pri">{run.agent_name}</td>
                <td className="mono dim">{scopeLabel(run)}</td>
                <td
                  className="mono dim"
                  title={run.evaluators.map((e) => evaluatorLabel(t, e)).join(", ")}
                >
                  {run.evaluators.length}
                </td>
                <td
                  className="mono"
                  style={{ color: run.scores.length ? "var(--good)" : "var(--ink-3)" }}
                >
                  {average(run)}
                </td>
                <td className="mono dim">
                  {run.created_at ? new Date(run.created_at).toLocaleString() : "—"}
                </td>
                <td>{statusChip(run)}</td>
              </tr>
            ))}
            {runs.length === 0 && (
              <tr>
                <td colSpan={7} className="dim mono" style={{ textAlign: "center" }}>
                  {t("evalPage.runs.empty")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <Pager
          always
          total={runTotal}
          page={runPage}
          size={runSize}
          onPage={setRunPage}
          onSize={(size) => {
            setRunSize(size);
            setRunPage(1);
          }}
        />
      </Panel>

      <div className="eval-grid">
        <Panel
          title={t("evalPage.scores.title")}
          sub={selectedRun ? `run-${selectedRun.id.slice(0, 6)} · ${selectedRun.agent_name}` : "—"}
          end={
            selectedRun &&
            selectedRun.session_ids.length > 0 &&
            agents.find((agent) => agent.id === selectedRun.agent_id)
              ?.experiment_capability.eligible ? (
                <Btn
                  data-testid="run-to-experiment"
                  onClick={() =>
                    setSearchParams({
                      view: "experiment",
                      exp: "new",
                      agent: selectedRun.agent_id,
                      sourceRun: selectedRun.id,
                    })
                  }
                >
                  {t("evalPage.runs.createExperiment")}
                </Btn>
              ) : undefined
          }
          style={{ "--i": 1 } as CSSProperties}
        >
          {selectedRun?.scores.length ? (
            <>
              {selectedRun.scores.map((score) => (
                <div className="hbar" key={score.evaluatorId}>
                  <span className="hn" title={score.evaluatorId}>
                    {evaluatorLabel(t, score.evaluatorId)}
                  </span>
                  <div className="track">
                    <div className="fill" style={{ width: `${score.score * 100}%` }} />
                  </div>
                  <span className="hv">{score.score.toFixed(2)}</span>
                </div>
              ))}
              <div className="note" style={{ marginTop: 6 }}>
                <span className="i">[i]</span>
                <span>{t("evalPage.scores.note")}</span>
              </div>
            </>
          ) : selectedRun ? (
            // A failed (or still running) run has no scores, and used to fall
            // through to "select a completed run" — hiding both what was applied
            // and why it failed, since the reason only ever flashed past in a
            // toast during polling.
            <div data-testid="run-detail">
              <div className="kv">
                <span className="k">{t("evalPage.newRun.mode")}</span>
                <span className="v">
                  {t(
                    selectedRun.mode === "insights"
                      ? "evalPage.newRun.modeInsights"
                      : "evalPage.newRun.modeEvaluators",
                  )}
                </span>
              </div>
              <div className="kv">
                <span className="k">{t("evalPage.runs.dataset")}</span>
                <span className="v">{scopeLabel(selectedRun)}</span>
              </div>
              <div className="kv">
                <span className="k">{t("evalPage.runs.sessions")}</span>
                <span className="v">{selectedRun.session_ids.length}</span>
              </div>
              {selectedRun.batch_eval_id && (
                <div className="kv">
                  <span className="k">{t("evalPage.runs.batch")}</span>
                  <span className="v">{selectedRun.batch_eval_id}</span>
                </div>
              )}
              <div className="kv" style={{ marginTop: 4 }}>
                <span className="k">
                  {t(
                    selectedRun.mode === "insights"
                      ? "evalPage.newRun.insightTypes"
                      : "evalPage.runs.applied",
                  )}
                </span>
              </div>
              <div className="selchips" style={{ marginBottom: 10 }}>
                {selectedRun.evaluators.map((id) => (
                  <span className="selchip" key={id} title={id}>
                    {evaluatorLabel(t, id)}
                  </span>
                ))}
              </div>
              {selectedRun.error ? (
                <div className="note" style={{ borderColor: "var(--crit)" }}>
                  <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                  <span>
                    {t("evalPage.runs.failureReason")}{" "}
                    <span className="mono" data-testid="run-error">
                      {selectedRun.error}
                    </span>
                  </span>
                </div>
              ) : (
                <div className="empty">{t("evalPage.scores.empty")}</div>
              )}
            </div>
          ) : (
            <div className="empty">{t("evalPage.scores.empty")}</div>
          )}
        </Panel>

        <Panel
          title={t("evalPage.insights.title")}
          sub={t("evalPage.insights.sub")}
          end={
            selectedRun && (
              <Btn
                disabled={
                  (selectedRun.session_ids?.length ?? 0) < 3 ||
                  insightsBusy ||
                  insightsPending(selectedRun)
                }
                title={
                  (selectedRun.session_ids?.length ?? 0) < 3
                    ? t("evalPage.insights.needSessions")
                    : insightsPending(selectedRun)
                      ? t("evalPage.insights.pendingDup")
                      : undefined
                }
                data-testid="insights-on-sessions-btn"
                onClick={() => setConfirmInsights(true)}
              >
                ↻ {t("evalPage.insights.runOnSessions")}
              </Btn>
            )
          }
          style={{ "--i": 2 } as CSSProperties}
        >
          {selectedRun && hasInsightTrees(selectedRun.insights) ? (
            <div style={{ maxHeight: 460, overflowY: "auto" }}>
              <InsightClusters insights={selectedRun.insights} />
            </div>
          ) : selectedRun?.error && selectedRun.status === "completed" ? (
            // COMPLETED_WITH_ERRORS: the run finished but the service returned
            // no trees (e.g. under 3 sessions — clustering minimum). Show why.
            // A FAILED run is not a partial result — its reason belongs to the
            // run-detail block on the left, not here.
            <div className="note" style={{ borderColor: "var(--warn)" }}>
              <span className="i" style={{ color: "var(--warn)" }}>[!]</span>
              <span>
                {t("evalPage.insights.partial")}{" "}
                <span className="mono">{selectedRun.error}</span>
              </span>
            </div>
          ) : (
            <div className="empty">{t("evalPage.insights.empty")}</div>
          )}
        </Panel>
      </div>

      <ConfirmDialog
        open={confirmInsights && !!selectedRun}
        title={t("evalPage.insights.confirmRun.title")}
        body={t(
          selectedRun && insightsAlreadyRan(selectedRun)
            ? "evalPage.insights.confirmRun.bodyRepeat"
            : "evalPage.insights.confirmRun.body",
          {
            run: selectedRun ? `run-${selectedRun.id.slice(0, 6)}` : "",
            count: selectedRun?.session_ids?.length ?? 0,
          },
        )}
        confirmLabel={t("evalPage.insights.confirmRun.confirm")}
        onConfirm={() => {
          setConfirmInsights(false);
          if (selectedRun) void startInsightsOnRun(selectedRun);
        }}
        onCancel={() => setConfirmInsights(false)}
      />
    </section>
  );
}

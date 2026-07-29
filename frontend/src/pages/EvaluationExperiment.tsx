import type { TFunction } from "i18next";
import type { CSSProperties } from "react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { FlaskConical, Gauge } from "lucide-react";
import { useSearchParams } from "react-router-dom";

import {
  Btn,
  Chip,
  ConfirmDialog,
  Pager,
  Panel,
  StageCard,
  useTablePage,
  useToast,
  ViewHead,
} from "../components";
import type { AgentInfo } from "../lib/api";
import { api } from "../lib/api";
import { evaluatorLabel } from "../lib/evaluators";
import { fmtScore } from "../lib/format";
import { RuntimeCanaryView } from "./EvaluationRuntimeCanary";

export interface ABMetric {
  label: string;
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
    };
    bundles?: {
      control: { bundle_id?: string; arn: string; version?: string };
      treatment: { bundle_id?: string; arn: string; version?: string };
    };
    gateway?: { gateway_id: string; gateway_url?: string; target_v1?: string;
      online_evaluators?: string[] };
    abtest?: { ab_test_id: string };
    traffic?: { sent: number; failed: number; dataset_id?: string;
      dataset_name?: string };
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

interface DatasetInfo {
  id: string;
  name: string;
  kind: string;
  item_count: number;
}

interface EvaluatorInfo {
  id: string;
  name?: string;
  level?: string;
  source: string;
  requires_ground_truth?: boolean;
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

// Side-by-side before/after panes; the after pane goes green when it differs
// (agentxray DiffView pattern — panel diff, deliberately not token-level).
function DiffPanes({ before, after, beforeLabel, afterLabel }: {
  before: string; after: string; beforeLabel: string; afterLabel: string;
}) {
  const changed = before.trim() !== after.trim();
  const pane: CSSProperties = {
    maxHeight: 180, overflow: "auto", whiteSpace: "pre-wrap",
    fontSize: 10.5, margin: 0,
  };
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
      <div>
        <div className="mono dim" style={{ fontSize: 9.5, marginBottom: 3 }}>
          {beforeLabel}
        </div>
        <pre className="code" style={pane}>{before || "—"}</pre>
      </div>
      <div>
        <div
          className="mono"
          style={{ fontSize: 9.5, marginBottom: 3, display: "flex",
                   justifyContent: "space-between",
                   color: changed ? "var(--good)" : undefined }}
        >
          <span>{afterLabel}</span>
          {changed && <span>CHANGED</span>}
        </div>
        <pre
          className="code"
          style={{ ...pane,
                   border: changed ? "1px solid rgba(63,185,80,.4)" : undefined }}
        >
          {after || "—"}
        </pre>
      </div>
    </div>
  );
}

export function ExperimentView({ onBack }: { onBack: () => void }) {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const canaryMode = searchParams.get("mode") === "canary";
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
        title={t("evalPage.experiment.title")}
        meta={canaryMode ? t("canaryPage.meta") : t("evalPage.experiment.meta")}
      />
      <div style={{ display: "flex", justifyContent: "space-between", gap: 12,
                    alignItems: "center", flexWrap: "wrap", marginBottom: 14 }}>
        <Btn onClick={onBack}>◂ {t("evalPage.backToRuns")}</Btn>
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
      </div>
      {canaryMode ? <RuntimeCanaryView /> : <ConfigurationExperimentView />}
    </section>
  );
}

function ConfigurationExperimentView() {
  const { t } = useTranslation();
  const toast = useToast();
  const [experiments, setExperiments] = useState<ExperimentInfo[]>([]);
  const [activeAgents, setActiveAgents] = useState<AgentInfo[]>([]);
  const [datasets, setDatasets] = useState<DatasetInfo[]>([]);
  const [evaluators, setEvaluators] = useState<EvaluatorInfo[]>([]);
  const [onlineEvaluators, setOnlineEvaluators] = useState<string[]>(ONLINE_EVAL_DEFAULT);
  const [busy, setBusy] = useState(false);
  const [startAgentId, setStartAgentId] = useState("");
  const [startError, setStartError] = useState<string | null>(null);
  const [trafficDatasetId, setTrafficDatasetId] = useState("");
  const [editedPrompt, setEditedPrompt] = useState<string | null>(null);
  const [editedToolJson, setEditedToolJson] = useState<string | null>(null);
  // recommend generators are separately selectable — prompt & tool
  // descriptions come from two different AgentCore recommendation jobs
  const [genSp, setGenSp] = useState(true);
  const [genTd, setGenTd] = useState(true);
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

  useEffect(() => {
    api
      .listAgents()
      .then((res) => {
        const active = res.agents.filter((a) => a.status === "active");
        const eligible = active.filter((a) => a.experiment_capability.eligible);
        setActiveAgents(active);
        setStartAgentId((prev) => prev || (eligible[0]?.id ?? ""));
      })
      .catch(() => {});
    // simulated datasets need an actor loop — the traffic stage only replays
    // plain prompt sets (legacy / predefined)
    fetch("/api/eval/datasets")
      .then((res) => (res.ok ? res.json() : { datasets: [] }))
      .then((body: { datasets: DatasetInfo[] }) =>
        setDatasets(body.datasets.filter((d) => d.kind !== "simulated")))
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
  }, [refresh]);

  const agents = activeAgents.filter((a) => a.experiment_capability.eligible);
  const unsupportedAgents = activeAgents.filter(
    (a) => !a.experiment_capability.eligible,
  );

  // "?exp=<id>" selects a row from the list (linkable, back-button friendly);
  // "?exp=new" opens the start form even while other experiments exist.
  const [searchParams, setSearchParams] = useSearchParams();
  const expParam = searchParams.get("exp");
  const creatingNew = expParam === "new";
  const exp = creatingNew
    ? null
    : (experiments.find((e) => e.id === expParam) ?? experiments[0] ?? null);
  const selectExp = (id: string | null) => {
    setSearchParams(id ? { view: "experiment", exp: id } : { view: "experiment" });
  };
  // per-experiment control state must not leak across row switches
  useEffect(() => {
    setTrafficDatasetId("");
    setEditedPrompt(null);
    setEditedToolJson(null);
    setGenSp(true);
    setGenTd(true);
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
        body: JSON.stringify({ agent_id: startAgentId }),
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

  const startForm = (label: string) => (
    <>
      <div className="field">
        <label>{t("evalPage.newRun.agent")}</label>
        <select
          className="input"
          value={startAgentId}
          data-testid="exp-agent-select"
          onChange={(e) => setStartAgentId(e.target.value)}
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
          disabled={busy || !startAgentId || hasRunning}
          data-testid="exp-start-btn"
          onClick={() => void onStart()}
        >
          ▸ {label}
        </Btn>
      </div>
    </>
  );

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
            {recTypeCheckbox(t("expPage.recTypePrompt"), genSp, setGenSp,
                             "rec-type-sp")}
            {recTypeCheckbox(t("expPage.recTypeTools"), genTd, setGenTd,
                             "rec-type-td", !toolDescriptionsSupported)}
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
            },
          })}
        </>
      )}
      {rec && (
        <>
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
            <div className="selchips" style={{ maxHeight: 132, overflowY: "auto" }}>
              {evaluators.map((e) => {
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
                  </button>
                );
              })}
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
            <option value="">{t("expPage.builtinPrompts")}</option>
            {datasets.map((d) => (
              <option key={d.id} value={d.id} style={{ background: "#141816" }}>
                {d.name} ({d.item_count})
              </option>
            ))}
          </select>
          {actionBtn("traffic", t("expPage.sendTraffic"), {
            primary: true,
            extra: trafficDatasetId ? { dataset_id: trafficDatasetId } : undefined,
          })}
        </div>
      )}
      {a.traffic && (
        <div className="mono dim" style={{ fontSize: 10 }}>
          sent {a.traffic.sent} · failed {a.traffic.failed}
          {a.traffic.dataset_name
            ? ` · ${t("expPage.datasetTag")} ${a.traffic.dataset_name}`
            : ` · ${t("expPage.builtinPrompts")}`}
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
          return (
            <div className="ab-metric" key={metric.label}>
              <div className="am-h">
                <span>{evaluatorLabel(t, metric.label)}</span>
                {delta != null && (
                  <span className="d">{delta >= 0 ? "+" : ""}{delta.toFixed(2)}</span>
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
              {verdict.avg_delta != null && `Δ ${verdict.avg_delta}`} · n={verdict.n ?? 0}
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
                    !creatingNew && exp?.id === e.id ? "rgba(255,176,0,.045)" : undefined,
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
        <Pager {...pagerProps} />
      </Panel>

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

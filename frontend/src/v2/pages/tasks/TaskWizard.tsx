import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useSearchParams } from "react-router-dom";

import { api, dlcApi, errorMessage, type EvaluatorRow, type V2LogStream, type V2Range } from "../../../lib/api";
import { evalCostApi, usd } from "../../../lib/api/evalCost";
import { costRefusal, refusalText } from "../../../lib/eval-cost-refusal";
import { storageFlag, withStorageConfirm } from "../../../lib/storage-confirm";
import { actorModelOptions, CLOUD_VALUE_PREFIX, defaultActorModel } from "../../../lib/evaluation";
import { builtinsNotInCatalog, evaluatorLabel, type EvaluatorLevel } from "../../../lib/evaluators";
import { type GoldenRunCriteria, goldenRunCriteria, isGoldenSplit } from "../../../lib/goldenRun";
import { useModelCatalog } from "../../../lib/model-catalog";
import { adoptModelDefault, STATIC_MODEL_DEFAULTS, useModelDefaultsArrival } from "../../../lib/model-defaults";
import { CUSTOM_MODEL_OPTION } from "../../../lib/models";
import { CustomUnavailableNote } from "../../EvaluatorPicker";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { INSIGHT_TYPES, insightLabel } from "../../online";
import { SERVICE_NAME_RE, type TaskMode } from "../../tasks";
import {
  Alert,
  Button,
  Card,
  Field,
  FilterSelect,
  FlowHeader,
  OptionCard,
  SearchInput,
  Select,
  Spin,
  Steps,
  Table,
  Tag,
} from "../../ui";
import { backTarget } from "./back";
import { LogSourceFields } from "./LogSourceFields";
import { LogStreamPicker } from "./LogStreamPicker";
import { isPassiveSource, passiveEstimateBody } from "./passiveCost";
import { SessionPicker } from "./SessionPicker";
import { StrategySwitch } from "./StrategySwitch";

type Source = "window" | "sessions" | "logs" | "dataset";
/** What is evaluated: a platform agent, or CloudWatch telemetry with no agent behind it. */
type Target = "agent" | "cloudwatch";

const LOOKBACKS = [1, 6, 24, 72, 168, 336];
const SESSION_RANGE: V2Range = "7d";
const MAX_EVALUATORS = 10;
/** Insight clustering needs at least this many sessions. */
const MIN_INSIGHT_SESSIONS = 3;

interface Draft {
  name: string;
  description: string;
  target: Target;
  agentId: string;
  /** cloudwatch target: the span service.name + the batch's input log groups */
  serviceName: string;
  logGroups: string[];
  mode: TaskMode;
  source: Source;
  lookbackHours: number;
  sessionIds: string[];
  /** sessions of the log streams picked for the 日志 source, listed over `logHours` */
  logSessionIds: string[];
  logHours: number;
  /** local dataset id, or `cloud:<datasetId>` */
  dataset: string;
  /** the model that plays the user of a simulated-persona dataset */
  actorModelId: string;
  evaluators: string[];
  /** insight types, insights mode only */
  insights: string[];
  /** pass^k: each dataset scenario runs this many times (1 = off) */
  repeats: number;
  confirmCost: boolean;
}

// a function, not a constant: the actor is the configured default once loaded
const emptyDraft = (): Draft => ({
  name: "",
  description: "",
  target: "agent",
  agentId: "",
  serviceName: "",
  logGroups: [],
  mode: "evaluators",
  source: "window",
  lookbackHours: 24,
  sessionIds: [],
  logSessionIds: [],
  logHours: 168,
  dataset: "",
  actorModelId: defaultActorModel(),
  evaluators: ["Builtin.Correctness", "Builtin.Helpfulness"],
  insights: [...INSIGHT_TYPES],
  repeats: 1,
  confirmCost: false,
});

/**
 * Prefill from `?from=run:<id>` (copy), or from hand-off params
 * (`?agent=<id>&dataset=<id>&evaluators=<id,id,…>`, e.g. the architect assistant's
 * next steps). A copy of an online config opens the continuous form (`OnlineEditor`).
 */
async function seedDraft(from: string | null, handoff: Handoff, copySuffix: string): Promise<Draft> {
  if (from?.startsWith("run:")) {
    const run = await api.getEvaluationRun(from.slice(4));
    const name = run.dataset_name ?? "";
    const copied = run.mode === "insights" ? { mode: "insights" as const, insights: run.evaluators } : { evaluators: run.evaluators };
    const target = run.log_source
      ? { target: "cloudwatch" as const, serviceName: run.log_source.service_name, logGroups: run.log_source.log_group_names }
      : { agentId: run.agent_id };
    const base: Draft = { ...emptyDraft(), name: `${run.name || run.agent_name}${copySuffix}`.slice(0, 64), description: run.description ?? "", ...target, ...copied };
    if (name.startsWith("window:")) return { ...base, source: "window", lookbackHours: parseInt(name.slice(7), 10) || 24 };
    if (name.startsWith("cloud:") && run.dataset_id) return { ...base, source: "dataset", dataset: `${CLOUD_VALUE_PREFIX}${run.dataset_id}` };
    if (run.dataset_id) return { ...base, source: "dataset", dataset: run.dataset_id };
    if (name.startsWith("logs:")) return { ...base, source: "logs", logSessionIds: run.session_ids };
    return { ...base, source: "sessions", sessionIds: run.session_ids };
  }
  const seeded: Draft = { ...emptyDraft(), agentId: handoff.agent ?? "" };
  if (handoff.evaluators.length) seeded.evaluators = handoff.evaluators;
  if (handoff.dataset) return { ...seeded, source: "dataset", dataset: handoff.dataset };
  return seeded;
}

interface Handoff {
  agent: string | null;
  dataset: string | null;
  evaluators: string[];
}

/** The one-off create form (`?view=new`): a batch evaluation run in two steps. */
export function TaskWizard() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const toast = useV2Toast();
  const from = params.get("from");
  const handoff: Handoff = {
    agent: params.get("agent"),
    dataset: params.get("dataset"),
    evaluators: (params.get("evaluators") ?? "").split(",").map((s) => s.trim()).filter(Boolean),
  };
  const seed = useLoad(
    () => seedDraft(from, handoff, t("v2.tasks.copySuffix")),
    `seed:${from}:${handoff.agent}:${handoff.dataset}:${handoff.evaluators.join(",")}`,
  );
  const agents = useLoad(() => api.listAgents(), "agents");
  const datasets = useLoad(() => api.v2Datasets(), "datasets");
  const evaluators = useLoad(() => api.v2Evaluators(), "evaluators");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [step, setStep] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [evalLevel, setEvalLevel] = useState("");
  const [evalQ, setEvalQ] = useState("");
  // a golden split's run is scored against its criteria: what was preselected for it
  const [criteriaPick, setCriteriaPick] = useState<(GoldenRunCriteria & { dataset: string }) | null>(null);
  // bumped by the trajectory picker's refresh: re-reads past the 60 s session cache
  const [sessionsForce, setSessionsForce] = useState(0);

  // The actor is seeded before the configured defaults may have arrived: adopt the
  // configured actor (whichever comes last, seed or defaults) unless the user picked one.
  const actorTouched = useRef(false);
  const [actorCustom, setActorCustom] = useState(false);
  useModelCatalog();
  const adoptActor = (d: Draft, configured: string | undefined): Draft => ({
    ...d,
    actorModelId: adoptModelDefault(
      d.actorModelId, STATIC_MODEL_DEFAULTS.actor_model_id, configured ?? "", actorTouched.current,
    ),
  });
  const modelDefaults = useModelDefaultsArrival((d) => {
    setDraft((cur) => cur && adoptActor(cur, d.actor_model_id));
  });
  useEffect(() => {
    if (seed.data && draft === null) setDraft(adoptActor(seed.data, modelDefaults?.actor_model_id));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- adoptActor reads refs/props only
  }, [seed.data, draft]);
  // a copied run or a handoff can carry a built-in this region does not offer: it is
  // not in the picker (so it could not be unticked) and would fail the whole batch
  const [prunedEvaluators, setPrunedEvaluators] = useState<string[]>([]);
  useEffect(() => {
    if (!draft || !evaluators.data) return;
    const dropped = builtinsNotInCatalog(draft.evaluators, evaluators.data.evaluators);
    if (dropped.length === 0) return;
    setDraft({ ...draft, evaluators: draft.evaluators.filter((id) => !dropped.includes(id)) });
    setPrunedEvaluators((prev) => [...prev, ...dropped]);
  }, [draft, evaluators.data]);

  const activeAgents = useMemo(() => (agents.data?.agents ?? []).filter((a) => a.status === "active"), [agents.data]);
  const agent = activeAgents.find((a) => a.id === draft?.agentId) ?? null;
  const sessions = useLoad(
    () =>
      draft && (draft.source === "sessions" || draft.source === "window") && agent
        ? api.obsSessions(SESSION_RANGE, sessionsForce > 0)
        : Promise.resolve(null),
    `wizard-sessions:${draft?.source}:${agent?.id ?? ""}:${sessionsForce}`,
  );
  const agentSessions = useMemo(
    () => (sessions.data?.sessions ?? []).filter((s) => agent && s.agent === agent.name),
    [sessions.data, agent],
  );
  // cloudwatch target: the service's sessions in the window, for the 链路 preview
  const cwService = draft?.serviceName.trim() ?? "";
  const cwReady = !!draft && draft.target === "cloudwatch" && SERVICE_NAME_RE.test(cwService) && draft.logGroups.length > 0;
  const cwWindow = useLoad(
    () => (cwReady && draft?.source === "window" ? api.v2LogSessions(cwService, draft.logGroups, draft.lookbackHours) : Promise.resolve(null)),
    `cw-window:${cwReady}:${draft?.source}:${cwService}:${draft?.logGroups.join("|")}:${draft?.lookbackHours}`,
  );

  // pass^k replays dataset scenarios against an agent; it has no meaning for past sessions.
  // Computed before the early returns below, so the hook order never changes.
  const repeatable =
    !!draft && draft.target === "agent" && draft.source === "dataset" && draft.mode === "evaluators" &&
    !!draft.dataset && !draft.dataset.startsWith(CLOUD_VALUE_PREFIX);
  const estimate = useLoad(
    () =>
      repeatable && draft?.agentId
        ? dlcApi.estimate({ agent_id: draft.agentId, dataset_id: draft.dataset, evaluators: draft.evaluators, repeats: draft.repeats })
        : Promise.resolve(null),
    `task-estimate:${repeatable}:${draft?.agentId}:${draft?.dataset}:${draft?.repeats}:${draft?.evaluators.join(",")}`,
  );
  // eval-cost §1.1: a passive run is priced (and confirmed) too; this form only creates
  // one-off runs — the continuous (online) config is priced by `OnlineEditor`
  const passiveBody = repeatable || !draft ? null : passiveEstimateBody({ ...draft, strategy: "history", sampling: 0 });
  const passiveKey = passiveBody ? JSON.stringify(passiveBody) : "none";
  const passiveEstimate = useLoad(
    () => (passiveBody ? evalCostApi.estimate(passiveBody) : Promise.resolve(null)),
    `task-passive-estimate:${passiveKey}`,
  );
  if (seed.error) return <Alert tone="error">{seed.error}</Alert>;
  if (!draft) return <Spin />;
  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch });

  const localDatasets = datasets.data?.datasets ?? [];
  const selectedDataset = localDatasets.find((d) => d.id === draft.dataset) ?? null;
  // persona scenarios: an LLM actor plays the user, so the run needs its model
  const simulated = selectedDataset?.kind === "simulated";
  const windowSessions = agentSessions.filter(
    (s) => !s.last || Date.now() - new Date(s.last).getTime() <= draft.lookbackHours * 3_600_000,
  );

  const allEvaluators = evaluators.data?.evaluators ?? [];
  const evalRows = allEvaluators.filter((e) => {
    if (evalLevel && e.level !== evalLevel) return false;
    const needle = evalQ.trim().toLowerCase();
    return !needle || `${e.id} ${e.name ?? ""} ${evaluatorLabel(t, e.id)}`.toLowerCase().includes(needle);
  });

  const validateStep0 = (): string | null => {
    if (!draft.name.trim()) return t("v2.tasks.errName");
    if (draft.target === "agent" && !draft.agentId) return t("v2.tasks.errAgent");
    if (draft.target === "cloudwatch" && !SERVICE_NAME_RE.test(draft.serviceName.trim())) return t("v2.tasks.cw.errService");
    if (draft.target === "cloudwatch" && draft.logGroups.length === 0) return t("v2.tasks.cw.errGroups");
    if (draft.source === "sessions" && draft.sessionIds.length === 0) return t("v2.tasks.errSessions");
    if (draft.source === "dataset" && !draft.dataset) return t("v2.tasks.errDataset");
    if (draft.source === "logs" && draft.logSessionIds.length === 0) return t("v2.tasks.errLogs");
    const picked = draft.source === "sessions" ? draft.sessionIds.length : draft.source === "logs" ? draft.logSessionIds.length : null;
    if (draft.mode === "insights" && picked !== null && picked < MIN_INSIGHT_SESSIONS) {
      return t("v2.tasks.errInsightSessions", { min: MIN_INSIGHT_SESSIONS });
    }
    return null;
  };

  const next = () => {
    const problem = validateStep0();
    setError(problem);
    if (!problem) setStep(1);
  };

  const submit = async () => {
    const insights = draft.mode === "insights";
    if (insights && draft.insights.length === 0) return setError(t("v2.tasks.errInsights"));
    if (!insights && draft.evaluators.length === 0) return setError(t("v2.tasks.errEvaluators"));
    if (!insights && draft.evaluators.length > MAX_EVALUATORS) return setError(t("v2.tasks.errTooMany", { max: MAX_EVALUATORS }));
    const priced = passiveBody ? passiveEstimate.data : null;
    if (!insights && priced?.over_limit) return setError(t("v3.evalCost.overLimitHint", { max: usd(priced.max_usd) }));
    if (!insights && priced?.confirm_required && !draft.confirmCost) return setError(t("v3.evalCost.confirmFirst"));
    setSubmitting(true);
    setError(null);
    try {
      const scope =
        draft.source === "window"
          ? { lookback_hours: draft.lookbackHours }
          : draft.source === "sessions"
            ? { session_ids: draft.sessionIds }
            : draft.source === "logs"
              ? { session_ids: draft.logSessionIds, session_source: "logs" as const }
              : draft.dataset.startsWith(CLOUD_VALUE_PREFIX)
              ? { cloud_dataset_id: draft.dataset.slice(CLOUD_VALUE_PREFIX.length) }
              : { dataset_id: draft.dataset, ...(simulated ? { actor_model_id: draft.actorModelId } : {}) };
      const run = await withStorageConfirm((confirm) => api.v2CreateRun({
        ...(draft.target === "cloudwatch"
          ? { log_source: { service_name: draft.serviceName.trim(), log_group_names: draft.logGroups } }
          : { agent_id: draft.agentId }),
        name: draft.name.trim(),
        description: draft.description || undefined,
        ...(insights ? { mode: "insights" as const, evaluators: [], insights: draft.insights } : { evaluators: draft.evaluators }),
        ...scope,
        ...(repeatable ? { repeats: draft.repeats } : {}),
        // a replay and a passive run (session_ids / lookback_hours) alike
        ...(!insights && (repeatable || isPassiveSource(draft.source)) ? { confirm_cost: draft.confirmCost } : {}),
        ...storageFlag(confirm),
      }));
      toast("success", t("v2.tasks.createdRun"));
      // the new task's Back returns to where the wizard was opened from
      setParams({ view: "detail", kind: "run", id: run.id }, { state: location.state });
    } catch (err) {
      const refusal = costRefusal(err);
      if (refusal?.kind === "confirm") set({ confirmCost: false });
      setError(refusal ? refusalText(t, refusal) : errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  // choosing a golden split preselects the evaluators its published criteria name
  // (a copied run or an ?evaluators= hand-off keeps its own: they never come through here)
  const chooseDataset = (id: string) => {
    set({ dataset: id });
    setCriteriaPick(null);
    const row = localDatasets.find((d) => d.id === id);
    if (!isGoldenSplit(row)) return;
    goldenRunCriteria(row.criteria_lineage_id)
      .then((pick) => {
        setCriteriaPick({ ...pick, dataset: id });
        if (pick.evaluatorIds.length === 0) return;
        setDraft((prev) => (prev && prev.dataset === id ? { ...prev, evaluators: pick.evaluatorIds.slice(0, MAX_EVALUATORS) } : prev));
      })
      .catch(() => setCriteriaPick(null)); // unreadable criteria: the selection stays as it was
  };
  const pickNote = criteriaPick && criteriaPick.dataset === draft.dataset && draft.source === "dataset"
    ? criteriaPick.version === null
      ? t("v2.tasks.criteriaUnpublished", { name: criteriaPick.name })
      : t("v2.tasks.criteriaPreselected", { name: criteriaPick.name, version: criteriaPick.version, count: criteriaPick.evaluatorIds.length })
    : null;

  const toggleEvaluator = (id: string) =>
    set({ evaluators: draft.evaluators.includes(id) ? draft.evaluators.filter((e) => e !== id) : [...draft.evaluators, id] });

  // without a platform agent there is nothing to invoke (dataset) and no
  // agent-attributed trajectory list (sessions): only passive CloudWatch scopes
  const cloudwatch = draft.target === "cloudwatch";
  const sourceCards: { key: Source; disabled?: boolean }[] = [
    { key: "window" },
    { key: "sessions", disabled: cloudwatch },
    { key: "logs" },
    { key: "dataset", disabled: cloudwatch },
  ];
  const chooseTarget = (target: Target) =>
    set({
      target,
      source: target === "cloudwatch" && (draft.source === "sessions" || draft.source === "dataset") ? "window" : draft.source,
      logSessionIds: [],
    });
  const toggleInsight = (id: string) =>
    set({ insights: INSIGHT_TYPES.filter((x) => (x === id ? !draft.insights.includes(id) : draft.insights.includes(x))) });
  // too few sessions to cluster — the batch would come back with no trees
  const fewSessions =
    draft.mode === "insights" &&
    ((draft.source === "window" && !sessions.loading && windowSessions.length < MIN_INSIGHT_SESSIONS) ||
      (draft.source === "dataset" && !!selectedDataset && selectedDataset.item_count < MIN_INSIGHT_SESSIONS) ||
      (cloudwatch && draft.source === "window" && !!cwWindow.data && cwWindow.data.streams.length < MIN_INSIGHT_SESSIONS));

  return (
    <>
      <FlowHeader
        title={t("v2.tasks.newTitle")}
        onBack={() => setParams(backTarget(location.state))}
        steps={
          <Steps
            steps={[t("v2.tasks.stepData"), draft.mode === "insights" ? t("v2.tasks.stepInsights") : t("v2.tasks.stepEvaluators")]}
            current={step}
            onSelect={setStep}
          />
        }
        end={
          <>
            <Button disabled={step === 0} onClick={() => setStep(0)}>
              {t("v2.common.prev")}
            </Button>
            {step === 0 ? (
              <Button kind="primary" onClick={next} testId="v2-task-next">
                {t("v2.common.next")}
              </Button>
            ) : (
              <Button kind="primary" disabled={submitting} onClick={() => void submit()} testId="v2-task-submit">
                {t("v2.tasks.submitRun")}
              </Button>
            )}
          </>
        }
      />
      {/* the switch carries only the agent; a CloudWatch target has none */}
      {step === 0 && <StrategySwitch value="history" agentId={cloudwatch ? null : draft.agentId} />}
      {error && <Alert tone="error">{error}</Alert>}

      {step === 0 && (
        <>
          <Card title={t("v2.tasks.basic")}>
            <div className="v2-form cols-2">
              <Field label={t("v2.tasks.cw.target")} full>
                <div className="v2-options" style={{ gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
                  <OptionCard
                    title={t("v2.tasks.cw.targetAgent")}
                    desc={t("v2.tasks.cw.targetAgentDesc")}
                    on={!cloudwatch}
                    onClick={() => chooseTarget("agent")}
                    testId="v2-task-target-agent"
                  />
                  <OptionCard
                    title={t("v2.tasks.cw.targetCw")}
                    desc={t("v2.tasks.cw.targetCwDesc")}
                    on={cloudwatch}
                    onClick={() => chooseTarget("cloudwatch")}
                    testId="v2-task-target-cloudwatch"
                  />
                </div>
              </Field>
              <Field label={t("v2.tasks.colName")} required full={cloudwatch}>
                <input className="v2-input" value={draft.name} maxLength={64} onChange={(e) => set({ name: e.target.value })} data-testid="v2-task-name" />
              </Field>
              {!cloudwatch && (
                <Field label={t("v2.tasks.colAgent")} required hint={agents.loading ? undefined : activeAgents.length === 0 ? t("v2.tasks.noAgents") : undefined}>
                  <Select
                    value={draft.agentId}
                    onChange={(v) => set({ agentId: v, sessionIds: [], logSessionIds: [] })}
                    testId="v2-task-agent"
                    placeholder={t("v2.common.choose")}
                    options={activeAgents.map((a) => ({ value: a.id, label: a.name }))}
                  />
                </Field>
              )}
              <Field label={t("v2.tasks.description")} full>
                <textarea className="v2-textarea" rows={2} value={draft.description} maxLength={1000} placeholder={t("v2.tasks.descPlaceholder")} onChange={(e) => set({ description: e.target.value })} />
              </Field>
              <Field label={t("v2.tasks.mode")} full>
                <div className="v2-options" style={{ gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
                  <OptionCard
                    title={t("v2.tasks.modeEvaluators")}
                    desc={t("v2.tasks.modeEvaluatorsDesc")}
                    on={draft.mode === "evaluators"}
                    onClick={() => set({ mode: "evaluators" })}
                    testId="v2-task-mode-evaluators"
                  />
                  <OptionCard
                    title={t("v2.tasks.modeInsights")}
                    desc={t("v2.tasks.modeInsightsDesc")}
                    on={draft.mode === "insights"}
                    onClick={() => set({ mode: "insights" })}
                    testId="v2-task-mode-insights"
                  />
                </div>
              </Field>
            </div>
          </Card>

          {cloudwatch && (
            <Card title={t("v2.tasks.cw.title")} sub={t("v2.tasks.cw.sub")} testId="v2-task-cw">
              <LogSourceFields
                serviceName={draft.serviceName}
                logGroups={draft.logGroups}
                onChange={(patch) => set({ ...patch, logSessionIds: [] })}
                onUseAgent={(agentId) => set({ target: "agent", agentId, logSessionIds: [] })}
              />
            </Card>
          )}

          <Card title={t("v2.tasks.dataConfig")}>
            <div className="v2-options">
              {sourceCards.map(({ key, disabled }) => (
                <OptionCard
                  key={key}
                  title={t(`v2.tasks.src.${key}`)}
                  desc={t(`v2.tasks.src.${key}Desc`)}
                  on={draft.source === key}
                  disabled={disabled}
                  badge={disabled ? <Tag tone="gray">{t("v2.tasks.cw.needsAgent")}</Tag> : undefined}
                  onClick={() => set({ source: key })}
                  testId={`v2-task-src-${key}`}
                />
              ))}
            </div>

            <div className="v2-grid-2" style={{ marginTop: 20 }}>
              <div>
                <h3 className="v2-sec-title" style={{ fontSize: 14 }}>
                  {t("v2.tasks.scope")}
                </h3>
                {draft.source === "window" || draft.source === "logs" ? (
                  <Field
                    label={draft.source === "logs" ? t("v2.tasks.logs.window") : t("v2.tasks.lookback")}
                    hint={draft.source === "logs" ? t("v2.tasks.logs.picked", { count: draft.logSessionIds.length }) : t("v2.tasks.lookbackHint")}
                  >
                    <Select
                      value={String(draft.source === "logs" ? draft.logHours : draft.lookbackHours)}
                      onChange={(v) => set(draft.source === "logs" ? { logHours: Number(v) } : { lookbackHours: Number(v) })}
                      testId="v2-task-lookback"
                      options={LOOKBACKS.map((h) => ({ value: String(h), label: t("v2.tasks.hours", { count: h }) }))}
                    />
                  </Field>
                ) : draft.source === "dataset" ? (
                  <Field label={t("v2.tasks.dataset")} required hint={selectedDataset && !selectedDataset.has_ground_truth ? t("v2.tasks.noGroundTruth") : undefined}>
                    <Select
                      value={draft.dataset}
                      onChange={(v) => chooseDataset(v)}
                      testId="v2-task-dataset"
                      placeholder={t("v2.common.choose")}
                      options={[
                        ...(draft.dataset && !selectedDataset ? [{ value: draft.dataset, label: draft.dataset }] : []),
                        ...localDatasets.map((d) => ({
                          value: d.id,
                          label: `${d.name} · ${t("v2.datasets.items", { count: d.item_count })}${d.kind === "simulated" ? ` · ${t("v2.datasets.kind.simulated")}` : ""}`,
                        })),
                      ]}
                    />
                  </Field>
                ) : (
                  <p className="v2-muted">{t("v2.tasks.sessionsPicked", { count: draft.sessionIds.length })}</p>
                )}
                {draft.source === "dataset" && simulated && (
                  <div style={{ marginTop: 16 }}>
                    <Field label={t("v2.tasks.actorModel")} required hint={t("v2.tasks.actorModelHint")}>
                      <Select
                        value={actorCustom ? CUSTOM_MODEL_OPTION : draft.actorModelId}
                        onChange={(v) => {
                          actorTouched.current = true;
                          setActorCustom(v === CUSTOM_MODEL_OPTION);
                          if (v !== CUSTOM_MODEL_OPTION) set({ actorModelId: v });
                        }}
                        testId="v2-task-actor-model"
                        mono
                        options={[
                          ...actorModelOptions(modelDefaults, draft.actorModelId).map((m) => ({ value: m, label: m })),
                          { value: CUSTOM_MODEL_OPTION, label: t("models.picker.custom") },
                        ]}
                      />
                      {actorCustom && (
                        <input
                          className="v2-input mono"
                          style={{ marginTop: 8 }}
                          data-testid="v2-task-actor-model-custom"
                          value={draft.actorModelId}
                          placeholder={t("models.picker.customPlaceholder", { example: defaultActorModel(modelDefaults) })}
                          onChange={(e) => set({ actorModelId: e.target.value.trim() })}
                        />
                      )}
                    </Field>
                  </div>
                )}
              </div>
            </div>
          </Card>

          <Card
            title={draft.source === "logs" ? t("v2.tasks.logs.title") : draft.source === "sessions" && !cloudwatch ? t("v2.tasks.sessions.title") : t("v2.tasks.preview")}
            sub={
              draft.source === "logs"
                ? t(cloudwatch ? "v2.tasks.cw.logsSub" : "v2.tasks.logs.sub")
                : draft.source === "sessions" && !cloudwatch
                  ? t("v2.tasks.sessions.sub")
                  : t("v2.tasks.previewSub")
            }
          >
            {cloudwatch && !cwReady ? (
              <p className="v2-muted">{t("v2.tasks.cw.previewPick")}</p>
            ) : cloudwatch && draft.source === "window" ? (
              <>
                <Alert>
                  {cwWindow.data
                    ? t(cwWindow.data.truncated ? "v2.tasks.cw.previewWindowMore" : "v2.tasks.cw.previewWindow", {
                        count: cwWindow.data.streams.length,
                        hours: draft.lookbackHours,
                        service: cwService,
                      })
                    : t("v2.common.loading")}
                </Alert>
                <Table
                  columns={[
                    { key: "sid", title: t("v2.traces.colSession"), render: (r: V2LogStream) => <span className="mono">{r.session_id}</span> },
                    { key: "traces", title: t("v2.pipelines.traceCount"), className: "num", render: (r: V2LogStream) => r.traces ?? "—" },
                    { key: "last", title: t("v2.pipelines.lastActive"), className: "nowrap", render: (r: V2LogStream) => fmtTime(r.last_event) },
                  ]}
                  rows={(cwWindow.data?.streams ?? []).slice(0, 8)}
                  rowKey={(r) => r.session_id ?? r.stream}
                  loading={cwWindow.loading}
                  error={cwWindow.error}
                  onRetry={cwWindow.reload}
                  empty={t("v2.tasks.cw.previewNone")}
                />
              </>
            ) : !cloudwatch && !agent ? (
              <p className="v2-muted">{t("v2.tasks.previewPickAgent")}</p>
            ) : draft.source === "logs" ? (
              <>
                {cloudwatch ? (
                  <LogStreamPicker
                    load={(hours, q) => api.v2LogSessions(cwService, draft.logGroups, hours, q)}
                    loadKey={`cw:${cwService}:${draft.logGroups.join("|")}`}
                    hint={t("v2.tasks.cw.keywordHint")}
                    hours={draft.logHours}
                    selected={draft.logSessionIds}
                    onChange={(logSessionIds) => set({ logSessionIds })}
                  />
                ) : (
                  <LogStreamPicker
                    load={(hours, q) => api.v2AgentLogStreams(agent?.id ?? "", hours, q)}
                    loadKey={`agent:${agent?.id}`}
                    hint={t("v2.tasks.logs.keywordHint")}
                    hours={draft.logHours}
                    selected={draft.logSessionIds}
                    onChange={(logSessionIds) => set({ logSessionIds })}
                  />
                )}
                {draft.logSessionIds.length > 0 && (
                  <div style={{ marginTop: 12 }}>
                    <Alert>{t("v2.tasks.logs.hintSelected")}</Alert>
                  </div>
                )}
              </>
            ) : draft.source === "dataset" ? (
              selectedDataset ? (
                <Table
                  columns={[
                    { key: "n", title: "#", width: 48, render: (r: { i: number }) => r.i + 1 },
                    { key: "input", title: simulated ? t("v2.datasets.simInput") : "Input", render: (r: { input: string }) => <span className="clip">{r.input}</span> },
                    {
                      key: "expected",
                      title: simulated ? t("v2.datasets.simGoal") : t("v2.datasets.expected"),
                      render: (r: { expected: string }) => (r.expected ? <span className="clip">{r.expected}</span> : "—"),
                    },
                  ]}
                  rows={selectedDataset.items.slice(0, 5).map((item, i) => {
                    const turns = item.turns as { input?: unknown; expected_response?: unknown }[] | undefined;
                    const goal = (item.actor_profile as { goal?: unknown } | undefined)?.goal;
                    return {
                      i,
                      input: String(turns?.[0]?.input ?? item.prompt ?? item.input ?? ""),
                      expected: String(turns?.[0]?.expected_response ?? item.expected ?? goal ?? ""),
                    };
                  })}
                  rowKey={(r) => String(r.i)}
                />
              ) : (
                <p className="v2-muted">{t("v2.tasks.previewPickDataset")}</p>
              )
            ) : draft.source === "sessions" ? (
              <SessionPicker
                agentId={agent?.id ?? ""}
                sessions={agentSessions}
                loading={sessions.loading}
                error={sessions.error}
                onRefresh={() => setSessionsForce((n) => n + 1)}
                selected={draft.sessionIds}
                onChange={(sessionIds) => set({ sessionIds })}
              />
            ) : (
              <>
                <Alert>{t("v2.tasks.previewWindow", { count: windowSessions.length, hours: draft.lookbackHours })}</Alert>
                <Table
                  columns={[
                    { key: "sid", title: t("v2.traces.colSession"), render: (s: (typeof agentSessions)[number]) => <span className="mono">{s.session_id}</span> },
                    { key: "traces", title: t("v2.pipelines.traceCount"), className: "num", render: (s: (typeof agentSessions)[number]) => s.traces },
                    {
                      key: "status",
                      title: t("v2.traces.colStatus"),
                      render: (s: (typeof agentSessions)[number]) => (s.errors ? <Tag tone="red">{t("v2.traces.error", { count: s.errors })}</Tag> : <Tag tone="green">{t("v2.traces.ok")}</Tag>),
                    },
                    { key: "last", title: t("v2.pipelines.lastActive"), className: "nowrap", render: (s: (typeof agentSessions)[number]) => fmtTime(s.last) },
                  ]}
                  rows={windowSessions.slice(0, 8)}
                  rowKey={(s) => s.session_id}
                  loading={sessions.loading}
                  error={sessions.error}
                  empty={t("v2.tasks.previewNoSessions")}
                />
              </>
            )}
          </Card>
        </>
      )}

      {step === 1 && draft.mode === "insights" && (
        <Card title={t("v2.tasks.pickInsights")} sub={t("v2.tasks.pickedCount", { count: draft.insights.length, max: INSIGHT_TYPES.length })}>
          <div className="v2-form">
            <Field label={t("evalPage.newRun.insightTypes")}>
              <div className="v2-checks">
                {INSIGHT_TYPES.map((id) => (
                  <label key={id} className="v2-check">
                    <input type="checkbox" checked={draft.insights.includes(id)} onChange={() => toggleInsight(id)} data-testid={`v2-task-insight-${id}`} />
                    {insightLabel(t, id)}
                  </label>
                ))}
              </div>
            </Field>
          </div>
          <div className="v2-stack" style={{ marginTop: 12 }}>
            <Alert>{draft.source === "dataset" ? t("evalPage.newRun.insightsHint") : t("evalPage.newRun.insightsWindowHint")}</Alert>
            {fewSessions && <Alert tone="warn">{t("v2.tasks.insightsFewSessions", { min: MIN_INSIGHT_SESSIONS })}</Alert>}
          </div>
        </Card>
      )}

      {step === 1 && repeatable && (
        <Card title={t("v2.tasks.passK")} sub={t("v2.tasks.passKSub")} testId="v2-task-passk">
          <div className="v2-form cols-2">
            <Field label={t("v2.tasks.repeats")} hint={t("v2.tasks.repeatsHint")}>
              <input
                className="v2-input"
                type="number"
                min={1}
                max={10}
                value={draft.repeats}
                onChange={(e) => set({ repeats: Math.max(1, Math.min(10, Number(e.target.value) || 1)), confirmCost: false })}
              />
            </Field>
            <Field label={t("v2.dlc.cost.estimate")} hint={t("v2.dlc.cost.basisHint")}>
              {estimate.loading ? (
                <Spin />
              ) : estimate.data ? (
                <span>
                  <b>{estimate.data.total_usd === null ? t("v2.dlc.cost.unknown") : `$${estimate.data.total_usd.toFixed(2)}`}</b>{" "}
                  {t("v2.dlc.cost.sessions", { sessions: estimate.data.sessions, minutes: estimate.data.duration_minutes ?? 0 })}
                  {estimate.data.unpriced && <Tag tone="orange">{t("v2.dlc.cost.unpriced")}</Tag>}
                  {estimate.data.over_limit && <Tag tone="red">{t("v2.dlc.cost.overLimit")}</Tag>}
                </span>
              ) : (
                "—"
              )}
            </Field>
          </div>
          {draft.repeats > 1 && <Alert tone="warn">{t("v2.dlc.release.passKCost", { k: draft.repeats })}</Alert>}
          {estimate.data?.confirm_required && (
            <label className="v2-check">
              <input type="checkbox" checked={draft.confirmCost} onChange={(e) => set({ confirmCost: e.target.checked })} />{" "}
              {t("v2.tasks.confirmCost", { usd: estimate.data.total_usd ?? "—" })}
            </label>
          )}
        </Card>
      )}

      {step === 1 && passiveBody && (
        <Card
          title={t("v3.evalCost.estimateTitle")}
          sub={passiveBody.scope === "online" ? t("v3.evalCost.perDay") : t("v3.evalCost.perRun")}
          testId="v2-task-cost"
        >
          {passiveEstimate.loading && !passiveEstimate.data ? (
            <Spin />
          ) : !passiveEstimate.data ? (
            <Alert tone="warn">{t("v3.evalCost.estimateUnavailable")}</Alert>
          ) : (
            <>
              <span>
                <b>
                  {passiveEstimate.data.total_usd === null ? t("v3.evalCost.unknown") : usd(passiveEstimate.data.total_usd)}
                  {passiveBody.scope === "online" && passiveEstimate.data.total_usd !== null && ` ${t("v3.evalCost.slashDay")}`}
                </b>{" "}
                {passiveBody.scope === "online"
                  ? t("v3.evalCost.sessionsPerDay", { count: passiveEstimate.data.sessions })
                  : t("v3.evalCost.sessionsRun", { count: passiveEstimate.data.sessions, minutes: passiveEstimate.data.duration_minutes ?? 0 })}
                {passiveEstimate.data.sessions_basis === "history_7d" && ` · ${t("v3.evalCost.sessionsFromHistory")}`}{" "}
                {(passiveEstimate.data.unpriced_models?.length ?? 0) > 0 && (
                  <Tag tone="orange">{t("v3.evalCost.unpricedCount", { count: passiveEstimate.data.unpriced_models?.length ?? 0 })}</Tag>
                )}
                {passiveEstimate.data.over_limit && <Tag tone="red">{t("v3.evalCost.overLimit", { max: usd(passiveEstimate.data.max_usd) })}</Tag>}
              </span>
              {passiveEstimate.data.over_limit && <Alert tone="error">{t("v3.evalCost.overLimitHint", { max: usd(passiveEstimate.data.max_usd) })}</Alert>}
              {passiveEstimate.data.confirm_required && !passiveEstimate.data.over_limit && (
                <label className="v2-check">
                  <input type="checkbox" checked={draft.confirmCost} onChange={(e) => set({ confirmCost: e.target.checked })} />{" "}
                  {t("v3.evalCost.confirm", { usd: usd(passiveEstimate.data.total_usd) })}
                </label>
              )}
            </>
          )}
        </Card>
      )}

      {step === 1 && draft.mode === "evaluators" && (
        <Card title={t("v2.tasks.pickEvaluators")} sub={t("v2.tasks.pickedCount", { count: draft.evaluators.length, max: MAX_EVALUATORS })}>
          <CustomUnavailableNote marker={evaluators.data?.custom_unavailable} />
          {pickNote && <Alert tone={criteriaPick?.version === null ? "warn" : "info"}>{pickNote}</Alert>}
          {prunedEvaluators.length > 0 && (
            <Alert tone="warn">
              {t("v2.tasks.prunedEvaluators", { names: prunedEvaluators.map((id) => evaluatorLabel(t, id)).join(", ") })}
            </Alert>
          )}
          <div className="v2-toolbar">
            <FilterSelect
              label={t("v2.evaluators.colLevel")}
              value={evalLevel}
              allLabel={t("v2.common.all")}
              onChange={setEvalLevel}
              options={(["SESSION", "TRACE", "TOOL_CALL"] as EvaluatorLevel[]).map((l) => ({ value: l, label: t(`v2.level.${l}`) }))}
            />
            <div className="end">
              <SearchInput value={evalQ} onChange={setEvalQ} placeholder={t("v2.evaluators.search")} />
            </div>
          </div>
          {draft.evaluators.length > 0 && (
            <div className="v2-tags" style={{ marginBottom: 12 }}>
              {draft.evaluators.map((id) => (
                <Tag key={id} tone="blue">
                  {evaluatorLabel(t, id)}
                </Tag>
              ))}
            </div>
          )}
          <Table
            columns={[
              {
                key: "sel",
                title: "",
                width: 36,
                render: (e: EvaluatorRow) => (
                  <input
                    type="checkbox"
                    aria-label={e.id}
                    checked={draft.evaluators.includes(e.id)}
                    disabled={!draft.evaluators.includes(e.id) && draft.evaluators.length >= MAX_EVALUATORS}
                    onChange={() => toggleEvaluator(e.id)}
                    data-testid={`v2-task-eval-${e.id}`}
                  />
                ),
              },
              {
                key: "name",
                title: t("v2.evaluators.colName"),
                render: (e: EvaluatorRow) => (
                  <>
                    {e.source === "custom" ? (e.name ?? e.id) : evaluatorLabel(t, e.id)}
                    <span className="sub mono">{e.id}</span>
                  </>
                ),
              },
              { key: "source", title: t("v2.evaluators.colSource"), render: (e: EvaluatorRow) => t(`v2.evaluators.source.${e.source}`) },
              { key: "level", title: t("v2.evaluators.colLevel"), render: (e: EvaluatorRow) => t(`v2.level.${e.level}`, { defaultValue: e.level }) },
              {
                key: "gt",
                title: t("v2.evaluators.colGroundTruth"),
                render: (e: EvaluatorRow) =>
                  e.requires_ground_truth ? (
                    <Tag tone={draft.source === "dataset" && selectedDataset?.has_ground_truth ? "green" : "orange"}>{t("v2.evaluators.needsGt")}</Tag>
                  ) : (
                    <span className="v2-muted">{t("v2.common.no")}</span>
                  ),
              },
            ]}
            rows={evalRows}
            rowKey={(e) => e.id}
            loading={evaluators.loading}
            error={evaluators.error}
            onRetry={evaluators.reload}
            selectedKey={null}
          />
          {draft.evaluators.some((id) => allEvaluators.find((e) => e.id === id)?.requires_ground_truth) &&
            !(draft.source === "dataset" && selectedDataset?.has_ground_truth) && (
              <div style={{ marginTop: 12 }}>
                <Alert tone="warn">{t("v2.tasks.gtWarning")}</Alert>
              </div>
            )}
        </Card>
      )}
    </>
  );
}

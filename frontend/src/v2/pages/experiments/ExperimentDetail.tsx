import { Gauge } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { DiffPanes } from "../../../components/DiffPanes";
import { api, errorMessage, type RecommendProviderInfo } from "../../../lib/api";
import { evaluationRunPresentation, type EvaluationRunInfo } from "../../../lib/evaluation";
import { evaluatorLabel, evaluatorPolarity } from "../../../lib/evaluators";
import {
  type ExperimentInfo,
  fmtP,
  loadRecPrefs,
  loadRecTypes,
  ONLINE_EVAL_DEFAULT,
  ONLINE_EVAL_MAX,
  type RecPrefs,
  saveRecPrefs,
  saveRecTypes,
  verdictLabel,
} from "../../../lib/experiments";
import { CUSTOM_MODEL_OPTION } from "../../../lib/models";
import { EvaluatorPicker } from "../../EvaluatorPicker";
import { fmtScore, fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, Descriptions, Field, FlowHeader, Spin, Table, Tag } from "../../ui";
import { EXPERIMENT_TONE, stageLabel } from "./common";
import { type CardState, StageCard } from "./StageCard";

const POLL_MS = 8000;
const FAST_POLL_MS = 2500;

export function ExperimentDetail({ id, hasRunning }: { id: string; hasRunning: boolean }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const [exp, setExp] = useState<ExperimentInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<"cleanup" | "promote" | null>(null);

  const refresh = useCallback(async () => {
    try {
      setExp(await api.v2Experiment(id));
      setLoadError(null);
    } catch (err) {
      setLoadError(errorMessage(err));
    }
  }, [id]);

  // always follow the row; an action running server-side moves its progress line faster
  const running = exp?.running_action ?? null;
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), running ? FAST_POLL_MS : POLL_MS);
    return () => window.clearInterval(timer);
  }, [refresh, running]);

  // catalogs for the stage forms
  const providers = useLoad(() => api.experimentProviders().catch(() => ({ providers: [] as RecommendProviderInfo[] })), "exp-providers");
  const evaluatorsLoad = useLoad(() => api.v2Evaluators(), "evaluators");
  const datasetsLoad = useLoad(() => api.v2Datasets(), "datasets");
  const agentId = exp?.agent_id ?? "";
  const sourceRunsLoad = useLoad<EvaluationRunInfo[]>(
    () =>
      agentId
        ? api.listEvaluationRuns({ agent_id: agentId, limit: 200 }).then((r) => r.runs.filter((run) => run.status === "completed" && !!run.batch_eval_id))
        : Promise.resolve([]),
    `exp-source-runs:${agentId}`,
  );
  const recProviders = providers.data?.providers ?? [];
  // online evaluation scores live traces: ground-truth matchers can never apply
  const onlineEvaluatorRows = useMemo(() => (evaluatorsLoad.data?.evaluators ?? []).filter((e) => !e.requires_ground_truth), [evaluatorsLoad.data]);
  const datasets = useMemo(() => (datasetsLoad.data?.datasets ?? []).filter((d) => d.kind !== "simulated"), [datasetsLoad.data]);
  const sourceRuns = useMemo(() => sourceRunsLoad.data ?? [], [sourceRunsLoad.data]);

  // per-experiment operator state (the component is keyed by id): generator
  // checkboxes and RECOMMEND pickers come back from their per-experiment stash
  const [genSp, setGenSp] = useState(() => loadRecTypes(id).sp);
  const [genTd, setGenTd] = useState(() => loadRecTypes(id).td);
  const [prefsSeeded, setPrefsSeeded] = useState(false);
  const [sourceRunId, setSourceRunId] = useState("");
  const [providerId, setProviderId] = useState("agentcore");
  const [modelChoice, setModelChoice] = useState("");
  const [modelCustom, setModelCustom] = useState("");
  const [editedPrompt, setEditedPrompt] = useState<string | null>(null);
  const [editedToolJson, setEditedToolJson] = useState<string | null>(null);
  const [onlineEvaluators, setOnlineEvaluators] = useState<string[]>(ONLINE_EVAL_DEFAULT);
  const [trafficDataset, setTrafficDataset] = useState("");

  useEffect(() => {
    if (!exp || prefsSeeded) return;
    const prefs = loadRecPrefs(exp.id);
    const last = exp.artifacts.recommend;
    setSourceRunId(prefs.source ?? last?.trace_source?.run_id ?? "");
    setProviderId(prefs.provider ?? last?.provider ?? last?.tool_provider ?? "agentcore");
    setModelChoice(prefs.model ?? "");
    setModelCustom(prefs.customModel ?? "");
    setPrefsSeeded(true);
  }, [exp, prefsSeeded]);

  useEffect(() => {
    setTrafficDataset((prev) => (datasets.some((d) => d.id === prev) ? prev : (datasets[0]?.id ?? "")));
  }, [datasets]);

  // a restored source that is not among this agent's pinnable runs is dropped
  useEffect(() => {
    if (!sourceRunId || sourceRunsLoad.loading || !sourceRunsLoad.data) return;
    if (!sourceRuns.some((r) => r.id === sourceRunId)) setSourceRunId("");
  }, [sourceRunId, sourceRuns, sourceRunsLoad.loading, sourceRunsLoad.data]);

  const persistPrefs = (patch: RecPrefs) =>
    saveRecPrefs(id, { source: sourceRunId, provider: providerId, model: modelChoice, customModel: modelCustom, ...patch });

  const onAction = async (action: string, extra?: Record<string, unknown>) => {
    setBusy(true);
    try {
      const res = await api.v2ExperimentAction(id, action, extra);
      setExp(res.experiment);
    } catch (err) {
      toast("error", t("common.actionFailed", { msg: errorMessage(err) }));
    } finally {
      setBusy(false);
      void refresh();
    }
  };

  if (!exp) {
    return loadError ? (
      <>
        <FlowHeader title={id} onBack={() => setParams({})} />
        <Alert tone="error">{loadError}</Alert>
      </>
    ) : (
      <Spin />
    );
  }

  const a = exp.artifacts;
  const rec = a.recommend;
  const verdict = a.verdict;
  const promotion = a.promote;
  const canary = a.canary;
  const terminal = exp.status === "cleaned" || exp.status === "failed";
  const locked = busy || !!exp.running_action;

  const currentPrompt = a.agent_meta?.system_prompt ?? "";
  const recToolDescs = Object.fromEntries(Object.entries(rec?.tool_descriptions ?? {}).filter(([k]) => k !== "_error"));
  const hasRecTools = Object.keys(recToolDescs).length > 0;
  const spFailed =
    rec != null &&
    (rec.system_prompt_error != null || (rec.system_prompt_status != null && (rec.system_prompt_status !== "COMPLETED" || rec.recommended_prompt == null)));
  const spDone = rec?.recommended_prompt != null && !spFailed;
  const tdRan = rec != null && (rec.tool_status != null || rec.tool_descriptions != null);
  const acceptedPrompt = rec?.accepted_prompt;
  const treatmentPrompt = acceptedPrompt ?? rec?.recommended_prompt ?? "";
  const acceptPromptValue = editedPrompt ?? (spFailed ? currentPrompt : (rec?.recommended_prompt ?? currentPrompt));
  const acceptBlocked = spFailed && acceptPromptValue.trim() === currentPrompt.trim();
  const toolDescriptionsSupported = a.agent_meta?.experiment_capability?.tool_descriptions ?? true;
  const wantTd = genTd && toolDescriptionsSupported;
  const knownTools = rec?.analyzed_tools && Object.keys(rec.analyzed_tools).length ? rec.analyzed_tools : (a.agent_meta?.tools ?? {});
  const hasKnownTools = Object.keys(knownTools).length > 0;

  const provider = recProviders.find((p) => p.id === providerId) ?? recProviders.find((p) => p.id === "agentcore");
  const providerIsAws = !provider || provider.id === "agentcore";
  const modelId = modelChoice === CUSTOM_MODEL_OPTION ? modelCustom.trim() : modelChoice || (provider?.default_model_id ?? "");
  const providerNeedsSource = !providerIsAws && !!provider?.requires_source && !sourceRunId;
  const providerModelMissing = !providerIsAws && modelChoice === CUSTOM_MODEL_OPTION && modelCustom.trim() === "";
  // non-null exactly when a 3rd-party provider is selected
  const thirdParty = provider && !providerIsAws ? provider : null;
  const providerExtra: Record<string, unknown> = thirdParty
    ? { recommend_provider: thirdParty.id, ...(modelId ? { recommend_model_id: modelId } : {}) }
    : {};

  const recommendDone = !!(acceptedPrompt || a.bundles);
  const activeCard = !recommendDone
    ? "recommend"
    : !a.bundles
      ? "bundles"
      : !a.gateway || !a.abtest
        ? "gwab"
        : !a.traffic
          ? "traffic"
          : !verdict
            ? "verdict"
            : "post";
  const cardState = (key: string, done: boolean): CardState => (done ? "done" : activeCard === key ? "active" : "pending");

  const insufficient = !!verdict?.verdict.includes("insufficient");
  const nonSignificant = verdict?.significant === false;
  const weak = insufficient || nonSignificant;
  const promotionComplete = !!(promotion?.deployment_id && promotion.ab_test_status === "STOPPED");
  const legacyPromotion = !!(promotion?.after_weights && !promotionComplete);
  const promotionRunning = exp.running_action === "promote";
  const promotionFailed = !promotionRunning && !!exp.error?.startsWith("promote: ");

  /** Button while pending → progress line; a stored `<action>: …` error turns it into a retry. */
  const actionButton = (action: string, label: string, opts: { primary?: boolean; disabled?: boolean; extra?: Record<string, unknown> } = {}) => {
    const isRunning = exp.running_action === action;
    const failed = !isRunning && !!exp.error?.startsWith(`${action}: `);
    return (
      <div className="v2-stack">
        <div>
          <Button
            kind={opts.primary && !failed ? "primary" : undefined}
            disabled={locked || opts.disabled}
            onClick={() => void onAction(action, opts.extra)}
            testId={`v2-exp-action-${action}`}
          >
            {isRunning ? t("expPage.running") : failed ? t("expPage.retry") : label}
          </Button>
        </div>
        {isRunning && <span className="v2-muted" data-testid="v2-exp-progress">{exp.progress ?? "…"}</span>}
        {failed && <Alert tone="error">{exp.error}</Alert>}
      </div>
    );
  };

  // ── RECOMMEND ────────────────────────────────────────────────────────────
  const sourceRun = sourceRuns.find((r) => r.id === sourceRunId);
  const runLabel = (run: EvaluationRunInfo) =>
    [
      run.mode === "insights" ? t("expPage.recSourceInsights") : t("expPage.recSourceEval"),
      run.dataset_name ?? run.id,
      evaluationRunPresentation(run).status === "completed_with_errors" ? t("expPage.readiness.runStatus.completed_with_errors") : null,
      run.created_at ? fmtTime(run.created_at) : run.id,
      run.session_ids.length ? t("expPage.recSourceSessions", { count: run.session_ids.length }) : null,
    ]
      .filter(Boolean)
      .join(" · ");

  const sourceControls = (
    <Field
      label={t("expPage.recSource")}
      hint={sourceRunId ? t("expPage.recSourcePinnedNote") : t("expPage.recSourceWindowNote")}
    >
      <select
        className="v2-select"
        value={sourceRunId}
        onChange={(e) => {
          setSourceRunId(e.target.value);
          persistPrefs({ source: e.target.value });
        }}
        data-testid="v2-exp-rec-source"
      >
        <option value="">{t("expPage.recSourceWindow")}</option>
        {sourceRuns.map((run) => (
          <option key={run.id} value={run.id}>
            {runLabel(run)}
          </option>
        ))}
      </select>
      {sourceRun && evaluationRunPresentation(sourceRun).status === "completed_with_errors" && (
        <Alert tone="warn">
          {t("evalPage.runs.partialResults")} {sourceRun.error}
        </Alert>
      )}
    </Field>
  );

  const providerControls =
    recProviders.length > 1 ? (
      <>
        <Field label={t("expPage.providerLabel")}>
          <select
            className="v2-select"
            value={provider?.id ?? "agentcore"}
            onChange={(e) => {
              setProviderId(e.target.value);
              setModelChoice("");
              setModelCustom("");
              persistPrefs({ provider: e.target.value, model: "", customModel: "" });
            }}
            data-testid="v2-exp-rec-provider"
          >
            {recProviders.map((p) => (
              <option key={p.id} value={p.id}>
                {p.label}
              </option>
            ))}
          </select>
        </Field>
        {thirdParty && thirdParty.models.length > 0 && (
          <Field label={t("expPage.providerModel")}>
            <select
              className="v2-select"
              value={modelChoice}
              onChange={(e) => {
                setModelChoice(e.target.value);
                persistPrefs({ model: e.target.value });
              }}
              data-testid="v2-exp-rec-model"
            >
              {thirdParty.models.map((m) => (
                <option key={m.model_id} value={m.model_id === thirdParty.default_model_id ? "" : m.model_id}>
                  {m.label}
                  {m.model_id === thirdParty.default_model_id ? ` · ${t("expPage.providerDefaultModel")}` : ""}
                </option>
              ))}
              <option value={CUSTOM_MODEL_OPTION}>{t("expPage.providerCustomModel")}</option>
            </select>
            {modelChoice === CUSTOM_MODEL_OPTION && (
              <input
                className="v2-input mono"
                style={{ marginTop: 8 }}
                placeholder="global.anthropic.claude-sonnet-5"
                value={modelCustom}
                onChange={(e) => {
                  setModelCustom(e.target.value);
                  persistPrefs({ customModel: e.target.value });
                }}
              />
            )}
          </Field>
        )}
        {thirdParty && (
          <Alert>
            {t("expPage.providerNote")}
            {wantTd && !thirdParty.supports.includes("tool_descriptions") ? ` ${t("expPage.providerToolNote")}` : ""}
          </Alert>
        )}
        {providerNeedsSource && <Alert tone="warn">{t("expPage.providerNeedsSource")}</Alert>}
      </>
    ) : null;

  const toolsPreview = (
    <Field label={t("expPage.toolsToAnalyze")}>
      {hasKnownTools ? (
        <Table
          columns={[
            { key: "n", title: t("v2.experiments.toolName"), render: (r: [string, string]) => <span className="mono">{r[0]}</span> },
            { key: "d", title: t("v2.experiments.toolDesc"), render: (r: [string, string]) => r[1] || "—" },
          ]}
          rows={Object.entries(knownTools)}
          rowKey={(r) => r[0]}
          density="dense"
        />
      ) : (
        <span className="v2-muted">{t("expPage.noDiscoveredTools")}</span>
      )}
    </Field>
  );

  const generate = (types: string[]) =>
    void onAction("recommend", {
      recommend_types: types,
      ...(sourceRunId ? { recommend_source_run_id: sourceRunId } : {}),
      ...providerExtra,
    });

  const acceptRecommendation = () => {
    if (acceptBlocked) return;
    // explicit always: an absent field would mean "accept the recommended descriptions"
    let toolDescs: Record<string, string> = {};
    const raw = editedToolJson ?? (hasRecTools ? JSON.stringify(recToolDescs, null, 2) : "");
    if (raw.trim()) {
      try {
        const parsed: unknown = JSON.parse(raw);
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("shape");
        toolDescs = Object.fromEntries(Object.entries(parsed as Record<string, unknown>).map(([k, v]) => [k, String(v)]));
      } catch {
        toast("error", t("expPage.invalidToolJson"));
        return;
      }
    }
    void onAction("accept", { accepted_prompt: acceptPromptValue, accepted_tool_descriptions: toolDescs });
  };

  const attribution = (() => {
    const who = rec?.provider ?? rec?.tool_provider;
    if (!rec || !who || who === "agentcore") return null;
    const revised = Object.keys(rec.tool_descriptions ?? {}).length;
    return [
      t("expPage.recProducedBy", {
        provider: who,
        model: rec.provider_model_id ?? rec.tool_provider_model_id ?? "—",
        count: rec.provider_meta?.evidence_sessions ?? rec.tool_provider_meta?.evidence_sessions ?? 0,
      }),
      rec.tool_provider && revised > 0 ? t("expPage.recProviderTools", { count: revised }) : null,
      rec.accepted_edited ? t("expPage.recProviderEdited") : null,
    ]
      .filter(Boolean)
      .join(" · ");
  })();

  const recommendCard = (
    <StageCard id="recommend" index={1} title={t("expPage.card.recommend")} state={cardState("recommend", recommendDone)}>
      {!rec ? (
        <div className="v2-form">
          <Alert>{t("expPage.recommendHint")}</Alert>
          <Field label={t("v2.experiments.recTypes")}>
            <div className="v2-checks">
              <label className="v2-check">
                <input
                  type="checkbox"
                  checked={genSp}
                  onChange={(e) => {
                    setGenSp(e.target.checked);
                    saveRecTypes(id, e.target.checked, genTd);
                  }}
                  data-testid="v2-exp-rec-sp"
                />
                {t("expPage.recTypePrompt")}
              </label>
              <label className={!toolDescriptionsSupported || !hasKnownTools ? "v2-check disabled" : "v2-check"}>
                <input
                  type="checkbox"
                  checked={wantTd}
                  disabled={!toolDescriptionsSupported || !hasKnownTools}
                  onChange={(e) => {
                    setGenTd(e.target.checked);
                    saveRecTypes(id, genSp, e.target.checked);
                  }}
                  data-testid="v2-exp-rec-td"
                />
                {t("expPage.recTypeTools")}
              </label>
            </div>
          </Field>
          <div className="v2-form cols-2">
            {sourceControls}
            <div className="v2-form">{providerControls}</div>
          </div>
          {toolDescriptionsSupported && (wantTd || !hasKnownTools) && toolsPreview}
          {!toolDescriptionsSupported && <span className="v2-muted">{t("expPage.toolBundleUnsupported")}</span>}
          {actionButton("recommend", t("expPage.generateRec"), {
            primary: true,
            disabled: (!genSp && !wantTd) || (genSp && (providerNeedsSource || providerModelMissing)),
            extra: {
              recommend_types: [...(genSp ? ["system_prompt"] : []), ...(wantTd ? ["tool_descriptions"] : [])],
              ...(sourceRunId ? { recommend_source_run_id: sourceRunId } : {}),
              // the provider only matters for the system-prompt generator
              ...(genSp ? providerExtra : {}),
            },
          })}
        </div>
      ) : (
        <div className="v2-form">
          {(rec.trace_source || attribution) && (
            <span className="v2-muted">
              {[
                rec.trace_source
                  ? rec.trace_source.kind === "batch_evaluation"
                    ? t("expPage.recSourceUsedPinned", {
                        kind: rec.trace_source.run_mode === "insights" ? t("expPage.recSourceInsights") : t("expPage.recSourceEval"),
                        id: rec.trace_source.batch_eval_id ?? rec.trace_source.run_id ?? "—",
                        count: rec.trace_source.session_count ?? 0,
                      })
                    : t("expPage.recSourceUsedWindow", { days: rec.trace_source.lookback_days ?? 7 })
                  : null,
                attribution,
              ]
                .filter(Boolean)
                .join(" · ")}
            </span>
          )}
          {spDone && (
            <Field label={t("v2.experiments.promptDiff")} hint={rec.explanation}>
              <DiffPanes before={currentPrompt} after={rec.recommended_prompt ?? ""} beforeLabel={t("expPage.currentLabel")} afterLabel={t("expPage.recommendedLabel")} />
            </Field>
          )}
          {spFailed && (
            <Alert tone="error">
              {rec.provider && rec.provider !== "agentcore"
                ? t("expPage.spRecFailedProvider", { provider: rec.provider, model: rec.provider_model_id ?? "—", msg: rec.system_prompt_error ?? "" })
                : t("expPage.spRecFailed", { status: rec.system_prompt_status ?? "FAILED", msg: rec.system_prompt_error ?? "" })}
            </Alert>
          )}
          {hasRecTools && (
            <Field label={t("expPage.toolRecLabel")} hint={rec.tool_explanation}>
              {/* an overlay on the current set (that is how bundles applies it) */}
              <DiffPanes
                before={JSON.stringify(rec.analyzed_tools ?? {}, null, 2)}
                after={JSON.stringify({ ...(rec.analyzed_tools ?? {}), ...recToolDescs }, null, 2)}
                beforeLabel={t("expPage.currentLabel")}
                afterLabel={t("expPage.recommendedLabel")}
              />
            </Field>
          )}
          {tdRan && !hasRecTools && (
            <Alert>
              {rec.tool_status === "no-tools"
                ? t("expPage.toolRecNoTools")
                : rec.tool_status === "no-tool-calls"
                  ? t("expPage.toolRecNoCalls", { msg: rec.tool_error ?? "" })
                  : rec.tool_status === "error"
                    ? t("expPage.toolRecFailed", { msg: rec.tool_error ?? "" })
                    : t("expPage.toolRecEmpty")}
            </Alert>
          )}
          {!recommendDone && (!spDone || !tdRan || !hasRecTools) && (
            <Card title={t("v2.experiments.regenerate")}>
              <div className="v2-form">
                <div className="v2-form cols-2">
                  {sourceControls}
                  <div className="v2-form">{!spDone && providerControls}</div>
                </div>
                {toolDescriptionsSupported && (!tdRan || !hasRecTools) && toolsPreview}
                <div className="v2-row">
                  {!spDone && (
                    <Button disabled={locked || providerNeedsSource || providerModelMissing} onClick={() => generate(["system_prompt"])} testId="v2-exp-regen-sp">
                      {t("expPage.genSp")}
                    </Button>
                  )}
                  {toolDescriptionsSupported && (!tdRan || !hasRecTools) && (
                    <Button
                      disabled={locked || !hasKnownTools || providerNeedsSource || providerModelMissing}
                      onClick={() => generate(["tool_descriptions"])}
                      testId="v2-exp-regen-td"
                    >
                      {t("expPage.genTd")}
                    </Button>
                  )}
                </div>
                {exp.running_action === "recommend" && <span className="v2-muted">{exp.progress ?? "…"}</span>}
                {exp.running_action !== "recommend" && exp.error?.startsWith("recommend: ") && <Alert tone="error">{exp.error}</Alert>}
              </div>
            </Card>
          )}
          {!acceptedPrompt && !a.bundles && (
            <>
              <Field label={t("v2.experiments.treatmentPrompt")} hint={t("expPage.editHint")} error={acceptBlocked ? t("expPage.acceptBlockedRecFailed") : null}>
                <textarea
                  className="v2-textarea"
                  rows={8}
                  value={acceptPromptValue}
                  onChange={(e) => setEditedPrompt(e.target.value)}
                  data-testid="v2-exp-accept-prompt"
                />
              </Field>
              {hasRecTools && (
                <Field label={t("expPage.toolDescs")}>
                  <textarea
                    className="v2-textarea code"
                    rows={6}
                    spellCheck={false}
                    value={editedToolJson ?? JSON.stringify(recToolDescs, null, 2)}
                    onChange={(e) => setEditedToolJson(e.target.value)}
                    data-testid="v2-exp-accept-tools"
                  />
                </Field>
              )}
              <div>
                <Button kind="primary" disabled={locked || acceptBlocked} onClick={acceptRecommendation} testId="v2-exp-accept">
                  {t("expPage.accept")}
                </Button>
              </div>
            </>
          )}
          {acceptedPrompt && (
            <Alert tone="success">
              {t("expPage.accepted")}
              {acceptedPrompt.trim() !== (rec.recommended_prompt ?? currentPrompt).trim() ? ` · ${t("v2.experiments.edited")}` : ""}
            </Alert>
          )}
        </div>
      )}
    </StageCard>
  );

  // ── BUNDLES · GATEWAY/AB · TRAFFIC ───────────────────────────────────────
  const bundlesCard = recommendDone && (
    <StageCard id="bundles" index={2} title={t("expPage.card.bundles")} state={cardState("bundles", !!a.bundles)}>
      <div className="v2-form">
        <DiffPanes before={currentPrompt} after={treatmentPrompt} beforeLabel={t("expPage.controlLabel")} afterLabel={t("expPage.treatmentLabel")} />
        {!a.bundles ? (
          actionButton("bundles", t("expPage.createBundles"), { primary: true })
        ) : (
          <Descriptions
            items={[
              { label: t("expPage.controlLabel"), value: <span className="mono">{`${a.bundles.control.bundle_id ?? a.bundles.control.arn} @ ${a.bundles.control.version ?? "1"}`}</span> },
              { label: t("expPage.treatmentLabel"), value: <span className="mono">{`${a.bundles.treatment.bundle_id ?? a.bundles.treatment.arn} @ ${a.bundles.treatment.version ?? "1"}`}</span> },
            ]}
          />
        )}
      </div>
    </StageCard>
  );

  const gwabCard = !!a.bundles && (
    <StageCard id="gwab" index={3} title={t("expPage.card.gwab")} state={cardState("gwab", !!a.abtest)}>
      <div className="v2-form">
        {!a.gateway && (
          <>
            <Alert>{t("expPage.onlineEvaluatorsHint", { max: ONLINE_EVAL_MAX })}</Alert>
            <EvaluatorPicker
              evaluators={onlineEvaluatorRows}
              loading={evaluatorsLoad.loading}
              error={evaluatorsLoad.error}
              onRetry={evaluatorsLoad.reload}
              selected={onlineEvaluators}
              onChange={setOnlineEvaluators}
              max={ONLINE_EVAL_MAX}
              testIdPrefix="v2-exp-online-eval"
            />
            {actionButton("gateway", t("expPage.createGateway"), {
              primary: true,
              disabled: onlineEvaluators.length === 0,
              extra: { online_evaluators: onlineEvaluators },
            })}
          </>
        )}
        {a.gateway && (
          <Descriptions
            items={[
              { label: "Gateway", value: <span className="mono">{a.gateway.gateway_id}</span> },
              { label: t("v2.experiments.target"), value: <span className="mono">{a.gateway.target_v1 ?? "—"}</span> },
              {
                label: t("expPage.onlineEvaluators"),
                value: (a.gateway.online_evaluators ?? []).map((e) => evaluatorLabel(t, e)).join(" + ") || "—",
              },
              { label: "A/B test", value: a.abtest ? <span className="mono">{a.abtest.ab_test_id}</span> : "—" },
            ]}
          />
        )}
        {a.gateway && !a.abtest && actionButton("abtest", t("expPage.createAbTest"), { primary: true })}
      </div>
    </StageCard>
  );

  const trafficCard = !!a.abtest && (
    <StageCard id="traffic" index={4} title={t("expPage.card.traffic")} state={cardState("traffic", !!a.traffic)}>
      {!a.traffic ? (
        <div className="v2-form cols-2">
          <Field label={t("expPage.datasetTag")}>
            <select className="v2-select" value={trafficDataset} onChange={(e) => setTrafficDataset(e.target.value)} data-testid="v2-exp-traffic-dataset">
              {datasets.length === 0 && <option value="">{t("expPage.noTrafficDataset")}</option>}
              {datasets.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.name} ({d.item_count})
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("v2.common.actions")}>
            {actionButton("traffic", t("expPage.sendTraffic"), { primary: true, disabled: !trafficDataset, extra: { dataset_id: trafficDataset } })}
          </Field>
        </div>
      ) : (
        <Descriptions
          items={[
            { label: t("v2.experiments.sent"), value: a.traffic.sent },
            { label: t("v2.experiments.failed"), value: a.traffic.failed },
            { label: t("expPage.datasetTag"), value: a.traffic.dataset_name ?? "—" },
          ]}
        />
      )}
    </StageCard>
  );

  // ── VERDICT · PROMOTE ────────────────────────────────────────────────────
  const promoteControls = (() => {
    if (promotionComplete) {
      return (
        <>
          <Tag tone="green">
            {t("expPage.promoted")} · v{promotion?.agent_version ?? "—"}
          </Tag>
          <Button
            onClick={() => setParams({ mode: "canary", canary: "new", champion: exp.agent_id, sourceExp: exp.id })}
            testId="v2-exp-handoff-canary"
          >
            <Gauge size={14} aria-hidden="true" />
            {t("canaryPage.handoff")}
          </Button>
        </>
      );
    }
    if (legacyPromotion) {
      return (
        <>
          <Tag tone="orange">
            {t("expPage.legacyShift")} · T1 {promotion?.after_weights?.T1 ?? 99}%
          </Tag>
          {promotionRunning ? (
            actionButton("promote", t("expPage.completePromotion"))
          ) : (
            <Button disabled={locked} onClick={() => setConfirm("promote")} testId="v2-exp-complete-promotion">
              {promotionFailed ? t("expPage.retry") : t("expPage.completePromotion")}
            </Button>
          )}
        </>
      );
    }
    if (promotionRunning || (promotionFailed && !weak)) return actionButton("promote", t("expPage.promote"), { primary: !weak });
    if (promotionFailed && weak) {
      return (
        <Button disabled={locked} onClick={() => setConfirm("promote")} testId="v2-exp-promote-retry">
          {t("expPage.retry")}
        </Button>
      );
    }
    // weak evidence demotes PROMOTE to a secondary, confirm-gated action
    return (
      <Button
        kind={weak ? undefined : "primary"}
        disabled={locked}
        onClick={() => (weak ? setConfirm("promote") : void onAction("promote"))}
        testId="v2-exp-promote"
      >
        {t("expPage.promote")}
      </Button>
    );
  })();

  const verdictCard = !!a.traffic && (
    <StageCard id="verdict" index={5} title={t("expPage.card.verdict")} state={cardState("verdict", !!verdict)}>
      {!verdict ? (
        <div className="v2-form">
          <Alert>{t("expPage.aggregationHint")}</Alert>
          {actionButton("verdict", t("expPage.monitorResults"), { primary: true })}
        </div>
      ) : (
        <div className="v2-form">
          <div className={`v2-verdict${weak ? " weak" : ""}`} data-testid="v2-exp-verdict">
            <div className="v2-row">
              <Tag tone={weak ? "orange" : "green"}>{verdictLabel(t, verdict)}</Tag>
              {verdict.avg_delta != null && <span title={t("expPage.avgDeltaHint")}>Δ {verdict.avg_delta}</span>}
              <span className="v2-muted">n={verdict.n ?? 0}</span>
              {verdict.significant === true && <Tag tone="green">{t("evalPage.experiment.significant")}</Tag>}
              {nonSignificant && <span className="v2-muted">{t("evalPage.experiment.nonsig.observed", { verdict: verdict.verdict })}</span>}
            </div>
            <div className="v2-row">{promoteControls}</div>
          </div>
          {verdict.metrics.length > 0 && (
            <Table
              columns={[
                {
                  key: "m",
                  title: t("v2.evaluators.colName"),
                  render: (m: (typeof verdict.metrics)[number]) => (
                    <>
                      {evaluatorLabel(t, m.label)}
                      {(m.polarity ?? evaluatorPolarity(m.label)) < 0 && (
                        <span className="sub" title={t("expPage.lowerIsBetterHint")}>
                          ↓ {t("expPage.lowerIsBetter")}
                        </span>
                      )}
                    </>
                  ),
                },
                { key: "c", title: t("expPage.controlLabel"), render: (m: (typeof verdict.metrics)[number]) => <MeanBar value={m.control.mean} tone="control" /> },
                { key: "v", title: t("expPage.treatmentLabel"), render: (m: (typeof verdict.metrics)[number]) => <MeanBar value={m.variants[0]?.mean ?? null} tone="treatment" /> },
                {
                  key: "d",
                  title: "Δ",
                  className: "num",
                  render: (m: (typeof verdict.metrics)[number]) => {
                    const variant = m.variants[0];
                    if (m.control.mean == null || variant?.mean == null) return "—";
                    const delta = variant.mean - m.control.mean;
                    const polarity = m.polarity ?? evaluatorPolarity(m.label);
                    const oriented = delta * polarity;
                    return (
                      <span
                        className={`v2-score ${oriented > 0 ? "good" : oriented < 0 ? "mid" : ""}`}
                        title={t(polarity < 0 ? "expPage.deltaLowerBetter" : "expPage.deltaHigherBetter")}
                      >
                        {delta >= 0 ? "+" : ""}
                        {delta.toFixed(2)}
                      </span>
                    );
                  },
                },
                {
                  key: "n",
                  title: "n",
                  className: "nowrap",
                  render: (m: (typeof verdict.metrics)[number]) => `${m.control.sampleSize ?? "—"} / ${m.variants[0]?.sampleSize ?? "—"}`,
                },
                {
                  key: "p",
                  title: "p",
                  className: "nowrap",
                  render: (m: (typeof verdict.metrics)[number]) => {
                    const v = m.variants[0];
                    return (
                      <span className="v2-row" style={{ flexWrap: "nowrap" }}>
                        {v?.pValue != null ? fmtP(v.pValue) : "—"}
                        {v?.isSignificant != null && (
                          <Tag tone={v.isSignificant ? "green" : "gray"}>
                            {v.isSignificant ? t("evalPage.experiment.significant") : t("evalPage.experiment.notSignificant")}
                          </Tag>
                        )}
                      </span>
                    );
                  },
                },
              ]}
              rows={verdict.metrics}
              rowKey={(m) => m.label}
            />
          )}
          {legacyPromotion && <Alert tone="warn">{t("expPage.legacyShiftHint")}</Alert>}
          {promotionFailed && (legacyPromotion || weak) && <Alert tone="error">{exp.error}</Alert>}
          {weak && promotionComplete && (
            <Alert tone="warn">
              {insufficient ? t("evalPage.experiment.insufficient.promotedContext") : t("evalPage.experiment.nonsig.promotedContext")}
            </Alert>
          )}
          {weak && !promotionComplete && (
            <Alert tone="warn">
              {t(insufficient ? "evalPage.experiment.insufficient.reason" : "evalPage.experiment.nonsig.reason")}
              <ul className="v2-list">
                {(["a1", "a2", "a3"] as const).map((k) => (
                  <li key={k}>{t(`evalPage.experiment.${insufficient ? "insufficient" : "nonsig"}.${k}`)}</li>
                ))}
              </ul>
            </Alert>
          )}
        </div>
      )}
    </StageCard>
  );

  const canaryWeights = canary?.after_weights ?? canary?.weights;
  const legacyCanary = canary && (
    <Alert tone="warn">
      <b>{t("expPage.legacyCanary.title")}</b>
      {canaryWeights && (
        <div>
          {t("v2.experiments.legacyCanaryWeights", {
            c: canaryWeights.C ?? 90,
            t1: canaryWeights.T1 ?? 10,
            stage: (canary.ramp_stage ?? 0) + 1,
          })}
        </div>
      )}
      <div>{t("expPage.legacyCanary.body")}</div>
    </Alert>
  );

  const cleanupRows = a.cleanup && (
    <Table
      columns={[
        { key: "c", title: t("v2.experiments.resource"), render: (r: { category: string; status: string }) => r.category },
        {
          key: "s",
          title: t("v2.tasks.colStatus"),
          render: (r: { category: string; status: string }) => <Tag tone={r.status === "deleted" ? "green" : r.status === "error" ? "red" : "gray"}>{r.status}</Tag>,
        },
      ]}
      rows={a.cleanup}
      rowKey={(r) => `${r.category}:${r.status}`}
      density="dense"
    />
  );

  const cleanupCard = (
    <StageCard id="cleanup" index={6} title={t("expPage.card.cleanup")} state={a.cleanup ? "done" : "pending"}>
      <div className="v2-form">
        <Alert>{t("v2.experiments.cleanupHint")}</Alert>
        {!a.cleanup && (
          <div>
            <Button kind="danger" disabled={locked} onClick={() => setConfirm("cleanup")} testId="v2-exp-cleanup">
              {exp.running_action === "cleanup" ? t("expPage.running") : t("expPage.cleanup")}
            </Button>
          </div>
        )}
        {exp.running_action === "cleanup" && exp.progress && <span className="v2-muted">{exp.progress}</span>}
        {cleanupRows}
      </div>
    </StageCard>
  );

  // ── page ─────────────────────────────────────────────────────────────────
  const pipeline: { key: string; label: string; done: boolean }[] = [
    { key: "recommend", label: t("expPage.card.recommend"), done: recommendDone },
    { key: "bundles", label: t("expPage.card.bundles"), done: !!a.bundles },
    { key: "gwab", label: t("expPage.card.gwab"), done: !!a.abtest },
    { key: "traffic", label: t("expPage.card.traffic"), done: !!a.traffic },
    { key: "verdict", label: t("expPage.card.verdict"), done: !!verdict },
    { key: "cleanup", label: t("expPage.card.cleanup"), done: !!a.cleanup },
  ];

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {exp.name}
            <Tag tone={EXPERIMENT_TONE[exp.status] ?? "gray"}>{t(`v2.experiments.status.${exp.status}`, { defaultValue: exp.status })}</Tag>
          </span>
        }
        onBack={() => setParams({})}
        end={
          <>
            <Button onClick={() => void refresh()}>{t("v2.common.refresh")}</Button>
            {terminal && (
              <Button kind="primary" disabled={hasRunning} onClick={() => setParams({ view: "new", agent: exp.agent_id })} testId="v2-exp-start-new">
                {t("evalPage.experiment.startNew")}
              </Button>
            )}
          </>
        }
      />
      {loadError && <Alert tone="warn">{loadError}</Alert>}
      <Card title={t("v2.taskDetail.overview")} testId="v2-exp-overview">
        <Descriptions
          items={[
            { label: t("v2.tasks.colName"), value: exp.name },
            { label: "ID", value: <span className="mono">{exp.id}</span> },
            { label: t("v2.tasks.colAgent"), value: exp.agent_name },
            { label: t("v2.experiments.colStage"), value: stageLabel(t, exp) },
            {
              label: t("v2.experiments.colVerdict"),
              value: (
                <span className="v2-row">
                  {verdictLabel(t, verdict)}
                  {verdict?.significant === true && <Tag tone="green">{t("evalPage.experiment.significant")}</Tag>}
                  {promotionComplete && <Tag tone="green">{`${t("expPage.promoted")} v${promotion?.agent_version ?? "—"}`}</Tag>}
                  {legacyPromotion && <Tag tone="orange">{`${t("expPage.legacyShift")} T1 ${promotion?.after_weights?.T1 ?? 99}%`}</Tag>}
                </span>
              ),
            },
            { label: t("v2.tasks.colCreated"), value: fmtTime(exp.created_at) },
          ]}
        />
      </Card>
      <Card title={t("v2.experiments.pipeline")} sub={terminal ? undefined : t("expPage.stepHint")}>
        <div className="v2-stages v2-stages-6">
          {pipeline.map((s, i) => {
            const state = s.done ? "succeeded" : !terminal && (activeCard === s.key || (s.key === "cleanup" && exp.running_action === "cleanup")) ? "running" : "pending";
            return (
              <div key={s.key} className={`v2-stage ${state}`}>
                <span className="n">{s.done ? "✓" : i + 1}</span>
                <div className="b">
                  <div className="t">{s.label}</div>
                </div>
              </div>
            );
          })}
        </div>
      </Card>

      {terminal ? (
        <Card title={t("v2.experiments.summary")} testId="v2-exp-summary">
          <div className="v2-form">
            {exp.error && <Alert tone="error">{exp.error}</Alert>}
            <Alert>{exp.status === "cleaned" ? t("evalPage.experiment.summary.cleaned") : t("evalPage.experiment.summary.failed")}</Alert>
            {weak && promotionComplete && (
              <Alert tone="warn">
                {insufficient ? t("evalPage.experiment.insufficient.promotedContext") : t("evalPage.experiment.nonsig.promotedContext")}
              </Alert>
            )}
            {exp.status === "failed" && !a.cleanup && (
              <div>
                <Button kind="danger" disabled={busy} onClick={() => setConfirm("cleanup")} testId="v2-exp-cleanup">
                  {t("expPage.cleanup")}
                </Button>
              </div>
            )}
            {cleanupRows}
          </div>
        </Card>
      ) : (
        <>
          {recommendCard}
          {bundlesCard}
          {gwabCard}
          {trafficCard}
          {verdictCard}
          {legacyCanary}
          {cleanupCard}
        </>
      )}

      <Confirm
        open={confirm === "cleanup"}
        title={t("expPage.confirmCleanup.title")}
        body={t("expPage.confirmCleanup.body")}
        confirmLabel={t("expPage.cleanup")}
        danger
        busy={busy}
        onConfirm={() => {
          setConfirm(null);
          void onAction("cleanup");
        }}
        onClose={() => setConfirm(null)}
      />
      <Confirm
        open={confirm === "promote"}
        title={t(legacyPromotion ? "expPage.confirmCompletePromotion.title" : "evalPage.experiment.nonsig.confirmPromote.title")}
        body={t(legacyPromotion ? "expPage.confirmCompletePromotion.body" : "evalPage.experiment.nonsig.confirmPromote.body")}
        confirmLabel={t(legacyPromotion ? "expPage.completePromotion" : "expPage.promote")}
        busy={busy}
        onConfirm={() => {
          setConfirm(null);
          void onAction("promote");
        }}
        onClose={() => setConfirm(null)}
      />
    </>
  );
}

function MeanBar({ value, tone }: { value: number | null; tone: "control" | "treatment" }) {
  return (
    <span className="v2-row" style={{ flexWrap: "nowrap" }}>
      <span className="v2-bar" style={{ width: 120 }}>
        <span style={{ width: `${Math.round((value ?? 0) * 100)}%`, background: tone === "control" ? "var(--v2-ink-3)" : "var(--v2-primary)" }} />
      </span>
      <span className="mono">{fmtScore(value)}</span>
    </span>
  );
}

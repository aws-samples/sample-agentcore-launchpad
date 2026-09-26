import { ExternalLink } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import {
  A2A_MODEL_SOURCE,
  A2A_SKILL_SEEDS,
  AGENT_NAME_RE,
  type AgentForm,
  type AgentFormCatalogs,
  agentFormValid,
  type AgentMethod,
  buildAgentSpec,
  byocIssues,
  defaultModelForMethod,
  emptyAgentForm,
  entrypointAfterUpload,
  filesystemIssues,
  gatewaySelectionsValid,
  resolveKb,
  sourceForMethod,
  sourceOnMethodSwitch,
} from "../../../lib/agent-spec";
import { api, ApiError, type ByocPythonVersion, errorMessage } from "../../../lib/api";
import { defaultModelFor, type ModelSource } from "../../../lib/models";
import { apiErrorRows, intOrNull, knobProblems, MAX_TOKENS_CEILING } from "../../../pages/create/presetSettings";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, FlowHeader, OptionCard, Steps, Tag } from "../../ui";
import "./agents.css";
import {
  BasicCard,
  ByocArtifactCard,
  ByocBasicCard,
  ByocEnvCard,
  ByocModelsCard,
  ContainerToolsCard,
  FilesystemCard,
  HarnessToolsCard,
  ProtocolCard,
  SdkCard,
  StrandsToolsCard,
} from "./MethodSections";
import { MemoryCard, ModelCard, type SectionProps, SkillsKbCard, type WizardCatalogs, type WizardUi } from "./wizardKit";
import { WizardReview } from "./WizardReview";

const METHODS: AgentMethod[] = ["harness", "zip_runtime", "container", "byoc"];
const isMethod = (m: string | null): m is AgentMethod => !!m && (METHODS as string[]).includes(m);

/** The draft a landing starts on, honouring the classic prefills:
 *  `method=` preselects a method, `gateway=` / `skill=` a Registry record. */
function initialDraft(params: URLSearchParams): { form: AgentForm; step: number } {
  const method = params.get("method");
  const gateway = params.get("gateway");
  const skill = params.get("skill");
  const form = emptyAgentForm(isMethod(method) ? method : "harness");
  if (gateway) form.selectedGateway = [gateway];
  if (skill) form.skills = [skill];
  return { form, step: isMethod(method) || gateway || skill ? 1 : 0 };
}

/**
 * Native V2 creation wizard for every form-driven method — managed Harness,
 * Strands (zip_runtime, HTTP or A2A), other Agent SDK (container) and bring your
 * own code (byoc). The form model, spec builder and per-method validation are the
 * classic wizard's (`lib/agent-spec.ts`), so both consoles post the same
 * `AgentSpecInput` for the same inputs.
 */
export function AgentWizard() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { can } = useAuth();
  const canDeploy = can("agents.deploy");
  const [initial] = useState(() => initialDraft(params));
  const [step, setStep] = useState(initial.step);
  const [form, setForm] = useState<AgentForm>(initial.form);
  const [ui, setUi] = useState<Omit<WizardUi, "customSkills">>({
    customModel: false,
    byocUpload: null,
    byocUploading: false,
  });
  const [customSkills, setCustomSkills] = useState<WizardUi["customSkills"]>([]);
  const [touched, setTouched] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<{ text: string; rows: string[] } | null>(null);
  // the staged zip, kept so a Python-version change can re-run the requirements
  // pre-resolve (re-staging the same bytes under the new target)
  const lastZip = useRef<File | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const set: SectionProps["set"] = (patch) =>
    setForm((prev) => ({ ...prev, ...(typeof patch === "function" ? patch(prev) : patch) }));
  const method = form.method;

  // Catalogs: a failed read leaves that section empty and never blocks the form.
  const attachables = useLoad(() => api.registryAttachables(), "attachables");
  const kbs = useLoad(() => api.listAttachableKnowledgeBases(), "kbs");
  const memories = useLoad(() => api.memoryResources(), "memories");
  const cat: WizardCatalogs = useMemo(
    () => ({
      loading: attachables.loading,
      gatewayTargets: (attachables.data?.mcp_servers ?? []).filter((m) => m.gateway),
      remoteMcp: (attachables.data?.mcp_servers ?? []).filter((m) => !m.gateway),
      skills: attachables.data?.skills ?? [],
      kbCatalog: kbs.data?.items ?? [],
      memories: memories.data?.items ?? [],
    }),
    [attachables.data, attachables.loading, kbs.data, memories.data],
  );
  const specCatalogs: AgentFormCatalogs = {
    gatewayTargets: cat.gatewayTargets,
    remoteMcp: cat.remoteMcp,
    storedGatewayConfig: {},
    kbInfo: (id) => resolveKb(id, cat.kbCatalog, []),
  };

  // Switching source re-seeds the model (and the byoc allowed-models list) to that
  // source's catalog default.
  // `forMethod` is the method being switched to — `method` still holds the old one.
  const applySource = (source: ModelSource, forMethod: AgentMethod = method) => {
    set({ modelSource: source, modelId: defaultModelForMethod(forMethod, source), byocModels: [defaultModelFor(source)] });
    setUi((prev) => ({ ...prev, customModel: false }));
  };
  const pickMethod = (next: AgentMethod) => {
    if (next === method) return;
    set({ method: next });
    applySource(sourceOnMethodSwitch(next, form.protocol), next);
  };
  const changeProtocol = (next: "http" | "a2a") => {
    if (next === "http") {
      set({ protocol: "http" });
      // leaving the A2A pin re-offers the method default
      applySource(sourceForMethod(method));
    } else {
      set((prev) => ({ protocol: "a2a", a2aSkills: prev.a2aSkills.length ? prev.a2aSkills : A2A_SKILL_SEEDS }));
      // the A2A template has no Mantle branch
      if (form.modelSource !== A2A_MODEL_SOURCE) applySource(A2A_MODEL_SOURCE);
    }
  };

  const uploadZip = async (file: File, python?: ByocPythonVersion) => {
    setUi((prev) => ({ ...prev, byocUploading: true }));
    try {
      const info = await api.uploadByocArtifact(file, python ?? form.byocPython);
      if (!alive.current) return;
      lastZip.current = file;
      setUi((prev) => ({ ...prev, byocUpload: info }));
      set((prev) => ({
        byocUploadId: info.upload_id,
        byocEntrypoint: entrypointAfterUpload(info.detected.entrypoint_candidates, prev.byocEntrypoint),
      }));
    } catch (err) {
      if (alive.current) toast("error", errorMessage(err));
    } finally {
      if (alive.current) setUi((prev) => ({ ...prev, byocUploading: false }));
    }
  };
  const changePython = (version: ByocPythonVersion) => {
    set({ byocPython: version });
    // the pre-resolve result is per-Python-version — re-check the staged zip
    if (lastZip.current && ui.byocUpload?.detected.has_requirements) void uploadZip(lastZip.current, version);
  };

  /* ── validation: the shared gate + per-field messages ───────────────── */
  const knobIssues =
    method === "harness"
      ? knobProblems({ max_tokens: form.maxTokens, max_iterations: form.maxIterations, timeout_seconds: form.timeoutSeconds }, t)
      : [];
  const valid = agentFormValid(form, specCatalogs, { knobIssues, byocUploading: ui.byocUploading });
  const problems = useMemo(() => {
    const out: Record<string, string> = {};
    if (!AGENT_NAME_RE.test(form.name)) out.name = t("v2.agents.wizard.errName");
    if (method === "byoc") {
      const b = byocIssues(form);
      if (b.models) out.byocModels = t("v2.agents.wizard.errByocModels");
      if (b.imageUri) out.byocImage = t("v2.agents.wizard.errByocImage");
      if (b.upload) out.byocUpload = t("v2.agents.wizard.errByocUpload");
      if (b.entrypoint) out.byocEntrypoint = t("v2.agents.wizard.errByocEntrypoint");
      return out;
    }
    if (!form.systemPrompt.trim()) out.prompt = t("v2.agents.wizard.errPrompt");
    if (!form.modelId.trim()) out.model = t("v2.agents.wizard.errModel");
    if (!gatewaySelectionsValid(form, specCatalogs)) out.gateway = t("v2.agents.wizard.errGateway");
    if (method === "harness") {
      const tokens = intOrNull(form.maxTokens);
      if (Number.isNaN(tokens) || (tokens !== null && (tokens < 1 || tokens > MAX_TOKENS_CEILING)))
        out.maxTokens = t("create.system.settings.errors.maxTokens", { max: MAX_TOKENS_CEILING });
      const iterations = intOrNull(form.maxIterations);
      if (iterations === null || Number.isNaN(iterations) || iterations < 1 || iterations > 100)
        out.maxIterations = t("create.system.settings.errors.maxIterations");
      const timeout = intOrNull(form.timeoutSeconds);
      if (timeout === null || Number.isNaN(timeout) || timeout < 10 || timeout > 3600)
        out.timeout = t("create.system.settings.errors.timeout");
    }
    if (method === "container") {
      const fs = filesystemIssues(form);
      if (fs.sessionMount || Object.keys(fs.rows).length || fs.duplicatePaths || fs.vpc) out.fs = "fs";
    }
    return out;
    // specCatalogs is derived from cat
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form, method, cat, t]);
  const err = (key: string) => (touched ? problems[key] : undefined);

  const toReview = () => {
    setError(null);
    setTouched(true);
    if (valid) setStep(2);
  };
  const next = () => (step === 0 ? setStep(1) : toReview());
  const select = (index: number) => (index < 2 ? setStep(index) : toReview());

  const submit = async () => {
    if (submitting || !valid) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await api.createAgent(buildAgentSpec(form, specCatalogs));
      toast("success", t("v2.agents.wizard.started", { name: res.agent.name }));
      setParams({ view: "detail", id: res.agent.id });
    } catch (e) {
      if (!alive.current) return;
      setError({ text: errorMessage(e), rows: e instanceof ApiError ? apiErrorRows(e.detail) : [] });
    } finally {
      if (alive.current) setSubmitting(false);
    }
  };

  const stepLabels = [t("v2.agents.wizard.stepMethod"), t("v2.agents.wizard.stepConfig"), t("v2.agents.wizard.stepReview")];
  const section = { form, set, cat, err };
  const skillsKb = (kbNote: string) => (
    <SkillsKbCard {...section} customSkills={customSkills} setCustomSkills={setCustomSkills} kbNote={kbNote} />
  );
  const modelCard = (extra: Partial<Parameters<typeof ModelCard>[0]> = {}) => (
    <ModelCard
      form={form}
      set={set}
      err={err}
      custom={ui.customModel}
      setCustom={(on) => setUi((prev) => ({ ...prev, customModel: on }))}
      applySource={applySource}
      showSource
      {...extra}
    />
  );

  return (
    <>
      <FlowHeader
        title={t("v2.agents.new")}
        onBack={() => setParams({})}
        steps={<Steps steps={stepLabels} current={step} onSelect={select} />}
        end={
          <>
            <Button disabled={step === 0 || submitting} onClick={() => setStep(step - 1)}>
              {t("v2.common.prev")}
            </Button>
            {step < 2 ? (
              <Button kind="primary" disabled={!canDeploy || ui.byocUploading} onClick={next} testId="v2-agent-wizard-next">
                {t("v2.common.next")}
              </Button>
            ) : (
              <Button kind="primary" disabled={submitting || !canDeploy || !valid} onClick={() => void submit()} testId="v2-agent-wizard-submit">
                {t("v2.agents.wizard.submit")}
              </Button>
            )}
          </>
        }
      />
      {!canDeploy && <Alert tone="warn">{t("v2.agents.noPermission")}</Alert>}
      {error && (
        <Alert tone="error">
          {error.text}
          {error.rows.length > 0 && (
            <ul className="v2-agents-errrows mono">
              {error.rows.map((row, i) => (
                <li key={i}>{row}</li>
              ))}
            </ul>
          )}
        </Alert>
      )}
      {step === 1 && touched && !valid && <Alert tone="error">{t("v2.agents.wizard.fixErrors")}</Alert>}

      {step === 0 && (
        <>
          <Card title={t("v2.agents.wizard.methodTitle")} sub={t("v2.agents.wizard.methodSubNative")}>
            <div className="v2-options">
              {METHODS.map((m) => (
                <OptionCard
                  key={m}
                  title={t(`v2.agents.wizard.method.${m}`)}
                  desc={t(`v2.agents.wizard.method.${m}Desc`)}
                  on={method === m}
                  onClick={() => pickMethod(m)}
                  badge={m === "harness" ? <Tag tone="blue">{t("v2.agents.wizard.recommended")}</Tag> : undefined}
                  testId={`v2-agent-method-${m}`}
                />
              ))}
            </div>
          </Card>
          <Card title={t("v2.agents.wizard.otherWays")}>
            <div className="v2-row">
              <Link to="/v2/assistant" className="v2-btn">
                {t("v2.agents.wizard.assistant")}
              </Link>
              <Link to="/create/studio" className="v2-btn">
                {t("v2.agents.wizard.studio")}
              </Link>
              <Link to="/agents/import" className="v2-btn">
                {t("v2.agents.import")}
              </Link>
              <Link to="/agents/new" className="v2-btn">
                <ExternalLink size={13} aria-hidden="true" />
                {t("v2.agents.wizard.presets")}
              </Link>
            </div>
          </Card>
        </>
      )}

      {step === 1 && method === "harness" && (
        <>
          <BasicCard {...section} />
          {modelCard({ knobs: true })}
          <HarnessToolsCard {...section} />
          {skillsKb(t("create.configure.kbNote"))}
          <MemoryCard {...section} loop />
        </>
      )}

      {step === 1 && method === "zip_runtime" && (
        <>
          <BasicCard {...section} />
          <ProtocolCard form={form} set={set} onProtocol={changeProtocol} />
          {modelCard(form.protocol === "a2a" ? { showSource: false, sourceNote: t("v2.agents.wizard.a2aSourcePinned") } : {})}
          <StrandsToolsCard {...section} />
          {skillsKb(t("create.configure.kbNoteDirect"))}
          <MemoryCard {...section} loop={false} note={t("create.configure.note")} />
        </>
      )}

      {step === 1 && method === "container" && (
        <>
          <BasicCard {...section} />
          <SdkCard form={form} set={set} />
          {modelCard({ showSource: false, sourceNote: t("v2.agents.wizard.claudeSourcePinned"), claudeOnly: true })}
          <ContainerToolsCard {...section} />
          {skillsKb(t("create.configure.kbNoteDirect"))}
          <FilesystemCard form={form} set={set} touched={touched} />
          <MemoryCard {...section} loop={false} note={t("create.configure.note")} />
        </>
      )}

      {step === 1 && method === "byoc" && (
        <>
          <ByocBasicCard form={form} set={set} err={err} />
          <ByocArtifactCard
            form={form}
            set={set}
            err={err}
            ui={{ ...ui, customSkills }}
            onUpload={(file) => void uploadZip(file)}
            onPython={changePython}
          />
          <ByocModelsCard form={form} set={set} err={err} applySource={applySource} />
          <ByocEnvCard form={form} set={set} />
        </>
      )}

      {step === 2 && <WizardReview form={form} cat={cat} ui={{ ...ui, customSkills }} />}
    </>
  );
}

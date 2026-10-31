import { ExternalLink } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { useProject } from "../../../workspace/project-context";
import { useProdLock } from "../../../workspace/useProdLock";
import {
  A2A_MODEL_SOURCE,
  A2A_SKILL_SEEDS,
  AGENT_NAME_RE,
  type AgentForm,
  type AgentFormCatalogs,
  agentFormValid,
  type AgentMethod,
  authToolIssues,
  buildAgentSpec,
  byocIssues,
  contextLayerValid,
  contextLayerVia,
  defaultModelForMethod,
  emptyAgentForm,
  entrypointAfterUpload,
  FILESYSTEM_METHODS,
  filesystemIssues,
  formFromStoredSpec,
  gatewaySelectionsValid,
  inboundValid,
  prefillKbPick,
  republishSpec,
  resolveKb,
  type StoredAgentExtras,
  sourceForMethod,
  sourceOnMethodSwitch,
  withContextVia,
} from "../../../lib/agent-spec";
import { type AgentInfo, api, ApiError, type ByocPythonVersion, errorMessage, type SharedTemplateInfo } from "../../../lib/api";
import { networkChoices, RUNTIME_NETWORK_PURPOSES } from "../../../lib/api/agent-network";
import { networkProfilesApi } from "../../../lib/api/projects";
import { useModelCatalog } from "../../../lib/model-catalog";
import { adoptModelDefault, useModelDefaultsArrival } from "../../../lib/model-defaults";
import { defaultModelFor, isCustomModelId, type ModelSource } from "../../../lib/models";
import { useUiVersion } from "../../../lib/ui-version";
import { useAgentProjectPick } from "../../../lib/useAgentProjectPick";
import { apiErrorRows, intOrNull, knobProblems, MAX_TOKENS_CEILING } from "../../../pages/create/presetSettings";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, FlowHeader, OptionCard, Steps, Tag } from "../../ui";
import "./agents.css";
import { projectSwitchFor, pruneToCatalogs, wizardBackTarget, wizardDoneTarget } from "./wizardReturn";
import { KnowledgeField } from "./InlineKb";
import { inlineKbBlock, useInlineKb } from "./useInlineKb";
import {
  BasicCard,
  ByocArtifactCard,
  ByocBasicCard,
  ByocBuiltinsCard,
  ByocEnvCard,
  ByocModelsCard,
  ContainerToolsCard,
  FilesystemCard,
  McpServerCard,
  HarnessToolsCard,
  NameField,
  ProtocolCard,
  SdkCard,
  StrandsToolsCard,
} from "./MethodSections";
import { NetworkSection } from "./NetworkSection";
import { ProjectCard } from "./ProjectCard";
import { TemplateGallery } from "./TemplateGallery";
import { formFromSharedTemplate, formFromTemplate } from "./templateForms";
import { GuardrailCard, MemoryCard, ModelCard, type SectionProps, SkillsKbCard, type WizardCatalogs, type WizardUi } from "./wizardKit";
import { contextPrefill } from "../contextLayer/attach";
import { sectionVisible } from "../contextLayer/state";
import { ContextLayerSection } from "./ContextLayerSection";
import { IdentityCard } from "./IdentityCard";
import { InboundCard } from "./InboundAuthFields";
import { ConversationOutputCard, ModelParamsCard, PromptTemplateCard, StrandsGroup } from "./StrandsSections";
import { groupIssues, STRANDS_GROUPS, type StrandsGroupKey, strandsReadouts } from "./strandsGroups";
import { WizardReview } from "./WizardReview";

const METHODS: AgentMethod[] = ["harness", "zip_runtime", "container", "byoc"];

/** The `problems` keys each Strands group holds — a flagged group opens itself. */
const GROUP_PROBLEMS: Record<StrandsGroupKey, string[]> = {
  basics: ["name", "prompt", "model"],
  tools: ["gateway", "identity", "sb.tools"],
  behaviour: ["sb.behaviour"],
  output: ["sb.output"],
  advanced: ["inbound", "fs"],
};
const isMethod = (m: string | null): m is AgentMethod => !!m && (METHODS as string[]).includes(m);

interface Draft {
  form: AgentForm;
  step: number;
  /** what a loaded agent carries besides the form (edit mode only) */
  extras: StoredAgentExtras | null;
  /** `kb=1`: preselect a knowledge base once the catalog lands (zip_runtime / container) */
  kbPrefill?: boolean;
}

/** Methods whose wizard honours `kb=1` here (the managed Harness one is /v3/create). */
const KB_PREFILL_METHODS: AgentMethod[] = ["zip_runtime", "container"];

/** Groups a fresh Strands form opens on; a loaded agent also opens every group it
 *  has settings in, so a re-publish shows what it carries. */
function initialGroups(form: AgentForm, edit: boolean): Set<StrandsGroupKey> {
  const open = new Set<StrandsGroupKey>(["basics", "tools"]);
  if (edit) {
    const readouts = strandsReadouts(form);
    if (readouts.behaviour) open.add("behaviour");
    if (readouts.output) open.add("output");
  }
  return open;
}

/** The draft a landing starts on, honouring the classic prefills:
 *  `method=` preselects a method, `gateway=` / `skill=` a Registry record. */
function initialDraft(params: URLSearchParams): Draft {
  const method = params.get("method");
  const gateway = params.get("gateway");
  const skill = params.get("skill");
  const form = emptyAgentForm(isMethod(method) ? method : "harness");
  // `method=byoc&protocol=mcp` — the "MCP server" entry: a byoc artifact hosted as
  // an MCP server and wired to the workspace Gateway
  if (method === "byoc" && params.get("protocol") === "mcp") form.protocol = "mcp";
  // the new-agent chooser's answers (storyline §3.6)
  if (method === "zip_runtime" && params.get("protocol") === "a2a") form.protocol = "a2a";
  if (gateway) form.selectedGateway = [gateway];
  if (skill) form.skills = [skill];
  // "Attach to agent" from the Context Layer page (`context_ns` / `context_name`)
  const context = contextPrefill(params);
  if (context) form.contextLayer = context;
  return {
    form,
    step: isMethod(method) || gateway || skill ? 1 : 0,
    extras: null,
    kbPrefill: params.get("kb") === "1" && KB_PREFILL_METHODS.includes(form.method),
  };
}

/** A template's form, still in the network the draft had (the preselected default
 *  profile) unless the template names one itself. */
function keepNetwork(cur: AgentForm, next: AgentForm): AgentForm {
  if (next.networkProfileId || next.networkVpc) return next;
  return { ...next, networkProfileId: cur.networkProfileId, networkVpc: cur.networkVpc };
}

/** An existing agent loaded for a re-publish: straight to the configure step. A
 *  `context_ns` prefill (Context Layer "attach") replaces its stored namespace and
 *  keeps its access mode. */
function editDraft(agent: AgentInfo, params: URLSearchParams): Draft {
  const { form, extras } = formFromStoredSpec(agent.method as AgentMethod, agent.name, agent.spec);
  const context = contextPrefill(params);
  if (context) form.contextLayer = withContextVia(context, contextLayerVia(form.contextLayer));
  return { form, step: 1, extras };
}

/**
 * Native V2 creation wizard for every form-driven method — managed Harness,
 * Strands (zip_runtime, HTTP or A2A), other Agent SDK (container) and bring your
 * own code (byoc). The form model, spec builder and per-method validation are the
 * classic wizard's (`lib/agent-spec.ts`), so both consoles post the same
 * `AgentSpecInput` for the same inputs.
 *
 * With `edit` it is the re-publish editor: the form starts from the stored spec,
 * the method step is skipped (name and method are immutable on the backend), and
 * the save redeploys a new version onto the same AgentCore resource, carrying the
 * stored fields no form input owns (`republishSpec`).
 */
export function AgentWizard({ edit }: { edit?: AgentInfo } = {}) {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const uiVersion = useUiVersion();
  const toast = useV2Toast();
  const { can } = useAuth();
  const prodLock = useProdLock();
  const canDeploy = can("agents.deploy") && !prodLock.locked;
  const [initial] = useState(() => (edit ? editDraft(edit, params) : initialDraft(params)));
  const [step, setStep] = useState(initial.step);
  const [form, setForm] = useState<AgentForm>(initial.form);
  const [ui, setUi] = useState<Omit<WizardUi, "customSkills">>({
    // a stored id the dropdown does not offer rides the "Custom model ID…" branch
    customModel: edit
      ? isCustomModelId(initial.form.modelId, initial.form.modelSource, initial.form.method === "container")
      : false,
    byocUpload: null,
    byocUploading: false,
  });
  const [customSkills, setCustomSkills] = useState<WizardUi["customSkills"]>(initial.extras?.customSkills ?? []);
  // A fresh form is seeded before the configured defaults may have loaded: adopt the
  // configured default for the method then, unless the member already chose a model.
  // The account catalog joins the model dropdowns when it lands.
  useModelCatalog();
  useModelDefaultsArrival(() => {
    if (edit || ui.customModel) return;
    setForm((cur) => ({
      ...cur,
      modelId: adoptModelDefault(cur.modelId, initial.form.modelId, defaultModelForMethod(cur.method, cur.modelSource), false),
    }));
  });
  const [touched, setTouched] = useState(false);
  // Harness quick setup: the default for a fresh create; a re-publish or a
  // gateway/skill prefill starts on the full form. The member can flip either way.
  const [canQuick] = useState(() => !edit && !params.get("gateway") && !params.get("skill"));
  const [fullForm, setFullForm] = useState(false);
  const [advOpen, setAdvOpen] = useState(false);
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
  // a re-publish reuses the stored byoc upload unless a new zip is staged
  const storedUploadId = initial.extras?.byocUploadId ?? null;
  useEffect(() => {
    if (!storedUploadId) return;
    void api
      .getByocUpload(storedUploadId)
      .then((info) => alive.current && setUi((prev) => ({ ...prev, byocUpload: prev.byocUpload ?? info })))
      .catch(() => {
        /* manifest gone — the member must upload a fresh zip to change code */
      });
  }, [storedUploadId]);

  // T38: `?template=<id>` arrives from the shared-template gallery. The entry is
  // loaded once and applied as defaults; what the publisher's environment had to supply
  // (knowledge bases, tools, skills, memory) is shown so the member supplies their own.
  const sharedTemplateId = edit ? null : params.get("template");
  const [templateNeeds, setTemplateNeeds] = useState<SharedTemplateInfo["requirements"]>([]);
  useEffect(() => {
    if (!sharedTemplateId) return;
    let cancelled = false;
    void api
      .sharedTemplates()
      .then(({ templates }) => {
        const entry = templates.find((row) => row.id === sharedTemplateId);
        if (cancelled || !entry) return;
        setForm((cur) => keepNetwork(cur, formFromSharedTemplate(entry)));
        setTemplateNeeds(entry.requirements);
        setStep(1);
      })
      .catch(() => {
        /* the gallery is a convenience: the wizard still works from scratch */
      });
    return () => {
      cancelled = true;
    };
  }, [sharedTemplateId]);

  // `?scenario=<key>` picks a T10 scenario template from outside the wizard (the V3
  // launch page hands over here for what its quick form does not cover). Applied
  // once, exactly as a click in the gallery would; an unknown key leaves step 0.
  const scenarioKey = edit ? null : params.get("scenario");
  useEffect(() => {
    if (!scenarioKey) return;
    let cancelled = false;
    void api
      .agentTemplates()
      .then(({ templates }) => {
        const entry = templates.find((row) => row.key === scenarioKey);
        if (cancelled || !entry) return;
        setForm((cur) => keepNetwork(cur, formFromTemplate(entry)));
        setFullForm(true);
        setStep(1);
      })
      .catch(() => {
        /* the gallery is a convenience: the wizard still works from scratch */
      });
    return () => {
      cancelled = true;
    };
  }, [scenarioKey]);

  // A new agent starts in the project's default network profile (projects §12.6),
  // when `GET /api/network-profiles` marks a choosable one default; a template picked
  // afterwards keeps it (`keepNetwork`), and the member can still switch to PUBLIC.
  const [defaultNetwork, setDefaultNetwork] = useState<string | null>(null);
  useEffect(() => {
    if (edit) return;
    let live = true;
    networkProfilesApi
      .list()
      .then(({ profiles }) => {
        const pick = networkChoices(profiles, "", RUNTIME_NETWORK_PURPOSES).find((c) => c.profile.is_default && !c.disabled);
        if (live && pick) setDefaultNetwork(pick.profile.id);
      })
      .catch(() => {
        /* no list: the form starts PUBLIC and the network step shows the error */
      });
    return () => {
      live = false;
    };
  }, [edit]);
  useEffect(() => {
    if (!defaultNetwork) return;
    setForm((cur) =>
      cur.networkProfileId || cur.networkVpc ? cur : { ...cur, networkProfileId: defaultNetwork, networkVpc: true },
    );
  }, [defaultNetwork]);

  const set: SectionProps["set"] = (patch) =>
    setForm((prev) => ({ ...prev, ...(typeof patch === "function" ? patch(prev) : patch) }));
  const method = form.method;
  // a NEW agent is filed under a business project the caller may create in, named
  // explicitly (body + X-Project) so a `default` global project never reaches the
  // create call; a re-publish keeps the agent's own project and loads nothing
  const project = useAgentProjectPick("agents.deploy", !edit);
  const projectId = project.projectId;
  const { current: consoleProject } = useProject();
  // the project the catalogs and the inline KB are read in: the picked one for a new
  // agent (what it may mount is that project's), the console's — the agent's own — for
  // an edit
  const scope = edit ? null : projectId || null;
  const kb = useInlineKb(set, scope, Boolean(edit) || Boolean(projectId));
  const [openGroups, setOpenGroups] = useState(() => initialGroups(initial.form, Boolean(edit)));
  const toggleGroup = (group: StrandsGroupKey) =>
    setOpenGroups((prev) => {
      const next = new Set(prev);
      if (next.has(group)) next.delete(group);
      else next.add(group);
      return next;
    });
  const quick = canQuick && method === "harness" && !fullForm;

  // Catalogs: a failed read leaves that section empty and never blocks the form.
  // each answer carries the project it was read in: on the render a switch happens the
  // loads still hold the previous project's lists
  const attachables = useLoad(
    () => api.registryAttachables(scope).then((d) => ({ ...d, scope })), `attachables:${scope ?? ""}`,
  );
  const kbs = useLoad(
    () => api.listAttachableKnowledgeBases(null, scope).then((d) => ({ ...d, scope })), `kbs:${scope ?? ""}`,
  );
  const memories = useLoad(() => api.memoryResources(scope), `memories:${scope ?? ""}`);
  // a project switch on a new agent: once the new project's catalogs answer, drop the
  // KBs and Skills it does not list (another project's — the agent could not mount
  // them) and an inline KB made in the previous project
  const prunedFor = useRef(scope);
  useEffect(() => {
    if (edit || prunedFor.current === scope) return;
    if (kbs.data?.scope !== scope || attachables.data?.scope !== scope) return;
    prunedFor.current = scope;
    if (kb.inline) kb.discard();
    setForm((prev) => ({ ...prev, ...pruneToCatalogs(prev, {
      kbIds: (kbs.data?.items ?? []).map((k) => k.kb_id),
      skillPaths: [
        ...(attachables.data?.skills ?? []).map((sk) => sk.path),
        ...customSkills.map((sk) => sk.path),
      ],
    }) }));
  }, [edit, scope, kbs.data, attachables.data, kb, customSkills]);
  // Unlike the catalogs above, a failed Connection read DOES block a spec whose
  // Identity rows name a Connection: nothing could verify it (authToolIssues).
  const connectionsLoad = useLoad(() => api.listConnections(), "connections");
  const connections = connectionsLoad.data?.connections ?? null;
  // what an "inherit" choice resolves to at deploy; a failed read reads as IAM
  const inboundDefault = useLoad(() => api.getInboundAuthDefault(), "inbound-default");
  // the workspace's COA context layer + the member's own authorization; a failed
  // read hides the section unless the stored spec already attaches a namespace
  const contextStatus = useLoad(() => api.getContextLayer(), "context-layer");
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
  // `kb=1` (chooser): once the catalog lands, preselect its first ACTIVE KB unless
  // the member already picked one, open the group holding the field and bring it
  // into view. Once per landing — clearing the pick afterwards is the member's call.
  const kbPrefilled = useRef(!initial.kbPrefill);
  useEffect(() => {
    if (kbPrefilled.current || kbs.loading || !KB_PREFILL_METHODS.includes(method)) return;
    kbPrefilled.current = true;
    set((prev) => ({ selectedKbs: prefillKbPick(prev.selectedKbs, kbs.data?.items ?? []) }));
    setOpenGroups((prev) => new Set(prev).add("tools"));
    window.setTimeout(() => {
      document.querySelector('[data-testid="v2-agent-kbs"]')?.scrollIntoView({ block: "center" });
    }, 0);
  }, [kbs.loading, kbs.data, method]);
  const specCatalogs: AgentFormCatalogs = {
    gatewayTargets: cat.gatewayTargets,
    remoteMcp: cat.remoteMcp,
    storedGatewayConfig: initial.extras?.storedGatewayConfig ?? {},
    // the inline KB is not in the catalog (loaded once, ACTIVE only): its ref comes
    // from the record so the spec still carries its name
    kbInfo: (id) =>
      resolveKb(id, cat.kbCatalog, [
        ...(initial.extras?.specKbs ?? []),
        ...(kb.inline ? [{ kb_id: kb.inline.kb_id, name: kb.inline.name, description: "" }] : []),
      ]),
  };

  // Switching source re-seeds the model (and the byoc allowed-models list) to that
  // source's catalog default.
  // `forMethod` is the method being switched to — `method` still holds the old one.
  const applySource = (source: ModelSource, forMethod: AgentMethod = method) => {
    const modelId = defaultModelForMethod(forMethod, source);
    set({ modelSource: source, modelId, byocModels: [defaultModelFor(source)] });
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
  const valid =
    agentFormValid(form, specCatalogs, {
      knobIssues,
      byocUploading: ui.byocUploading,
      connections,
    }) && (Boolean(edit) || Boolean(projectId));
  // a KB created here must exist (ACTIVE) to be mounted; its ingestion never blocks
  const kbBlock = inlineKbBlock(kb.inline, form.selectedKbs);
  const problems = useMemo(() => {
    const out: Record<string, string> = {};
    if (!AGENT_NAME_RE.test(form.name)) out.name = t("v2.agents.wizard.errName");
    if (!edit && !projectId) out.project = t("v3.create.project.err");
    if (!inboundValid(form)) out.inbound = "inbound";
    if (!contextLayerValid(form)) out.contextLayer = "contextLayer";
    if (form.authTools.length) {
      const auth = authToolIssues(form, connections);
      if (auth.unsupported || auth.catalogPending || Object.keys(auth.rows).length) out.identity = "identity";
    }
    if (method === "byoc") {
      const b = byocIssues(form);
      if (b.models) out.byocModels = t("v2.agents.wizard.errByocModels");
      if (b.imageUri) out.byocImage = t("v2.agents.wizard.errByocImage");
      if (b.upload) out.byocUpload = t("v2.agents.wizard.errByocUpload");
      if (b.entrypoint) out.byocEntrypoint = t("v2.agents.wizard.errByocEntrypoint");
      if (b.mcpTarget) out.mcpTarget = t("v2.agents.wizard.mcp.errTarget");
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
    if (method === "zip_runtime") {
      for (const group of groupIssues(form)) out[`sb.${group}`] = "strands";
    }
    if (FILESYSTEM_METHODS.includes(method)) {
      const fs = filesystemIssues(form);
      if (fs.sessionMount || Object.keys(fs.rows).length || fs.duplicatePaths || fs.vpc) out.fs = "fs";
    }
    return out;
    // specCatalogs is derived from cat
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form, method, cat, connections, projectId, t]);
  // after `problems`: reading it earlier hits the TDZ and crashes every render
  const readouts = strandsReadouts(form);
  const advancedProblem = ["model", "maxTokens", "maxIterations", "timeout", "gateway"].some((k) => problems[k]);
  const err = (key: string) => (touched ? problems[key] : undefined);

  const toReview = () => {
    setError(null);
    setTouched(true);
    if (valid) setStep(2);
  };
  const next = () => (step === 0 ? setStep(1) : toReview());
  const select = (index: number) => (index < 2 ? setStep(index) : toReview());
  // edit has no method step: its Steps show configure + review only
  const firstStep = edit ? 1 : 0;

  const submit = async () => {
    if (submitting || !valid || kbBlock || kb.busy) return;
    setSubmitting(true);
    setError(null);
    try {
      const built = buildAgentSpec(form, specCatalogs);
      const res = edit
        ? await api.redeployAgent(edit.id, republishSpec(built, edit.spec))
        : await api.createAgent({ ...built, project_id: projectId });
      toast("success", t(edit ? "v2.agents.wizard.redeployStarted" : "v2.agents.wizard.started", { name: res.agent.display_name || res.agent.name }));
      // in V3 the agent's V3 detail page, replacing the wizard so "back" from
      // there returns to the page that started it (§4)
      // filed under another project than the console's: land there, or the detail
      // page reads the new agent in the current project and finds nothing
      const switchTo = edit ? null : projectSwitchFor(projectId, consoleProject?.id);
      const done = wizardDoneTarget(uiVersion, res.agent.id, switchTo);
      if (done) navigate(done, { replace: true });
      else setParams({ view: "detail", id: res.agent.id, ...(switchTo ? { project: switchTo } : {}) });
    } catch (e) {
      if (!alive.current) return;
      setError({ text: errorMessage(e), rows: e instanceof ApiError ? apiErrorRows(e.detail) : [] });
    } finally {
      if (alive.current) setSubmitting(false);
    }
  };

  const stepLabels = [t("v2.agents.wizard.stepMethod"), t("v2.agents.wizard.stepConfig"), t("v2.agents.wizard.stepReview")].slice(firstStep);
  // a converted agent's exported code bakes its prompt and model (the backend refuses
  // changing either on re-publish), so both are shown read-only
  const converted = Boolean((edit?.spec as { code_bundle?: unknown } | undefined)?.code_bundle);
  const section = { form, set, cat, err, nameLocked: Boolean(edit), promptLocked: converted };
  const kbField = (note: string) => <KnowledgeField form={form} set={set} cat={cat} kb={kb} note={note} />;
  const skillsKb = (kbNote?: string) => (
    <SkillsKbCard
      {...section}
      customSkills={customSkills}
      setCustomSkills={setCustomSkills}
      kbSlot={kbNote === undefined ? undefined : kbField(kbNote)}
    />
  );
  const contextCard = sectionVisible(contextStatus.data, form) ? (
    <ContextLayerSection
      form={form}
      set={set}
      status={contextStatus.data}
      onStatusChanged={contextStatus.reload}
      touched={touched}
    />
  ) : null;
  const inboundCard = (
    <InboundCard
      form={form}
      set={set}
      touched={touched}
      workspaceDefault={inboundDefault.data?.default ?? null}
      cognito={inboundDefault.data?.cognito ?? null}
      cognitoIssuer={inboundDefault.data?.cognito_issuer ?? null}
    />
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
        title={
          edit
            ? t("v2.agents.wizard.editTitle", { name: edit.display_name || edit.name })
            : t(form.protocol === "mcp" ? "v2.agents.wizard.mcp.title" : "v2.agents.new")
        }
        onBack={() => {
          const back = wizardBackTarget(uiVersion, location.key, edit?.id);
          if (back === -1) navigate(-1);
          else if (back) navigate(back);
          else setParams(edit ? { view: "detail", id: edit.id } : {});
        }}
        steps={<Steps steps={stepLabels} current={step - firstStep} onSelect={(i) => select(i + firstStep)} />}
        end={
          <>
            <Button disabled={step === firstStep || submitting} onClick={() => setStep(step - 1)}>
              {t("v2.common.prev")}
            </Button>
            {step < 2 ? (
              <Button kind="primary" disabled={!canDeploy || ui.byocUploading || kb.busy} onClick={next} testId="v2-agent-wizard-next">
                {t("v2.common.next")}
              </Button>
            ) : (
              <Button kind="primary" disabled={submitting || !canDeploy || !valid || Boolean(kbBlock) || kb.busy} onClick={() => void submit()} testId="v2-agent-wizard-submit">
                {t(edit ? "v2.agents.wizard.redeploy" : "v2.agents.wizard.submit")}
              </Button>
            )}
          </>
        }
      />
      {!canDeploy && <Alert tone="warn">{prodLock.title ?? t("v2.agents.noPermission")}</Alert>}
      {edit && <Alert>{t("v2.agents.wizard.editNote")}</Alert>}
      {step === 1 && templateNeeds.length > 0 && (
        <Alert tone="warn">
          {t("v2.agents.wizard.templateNeeds", {
            items: templateNeeds.map((need) => need.label).join(", "),
          })}
        </Alert>
      )}
      {converted && <Alert>{t("v2.agents.wizard.convertedNote")}</Alert>}
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
          <TemplateGallery
            onPick={(template) => {
              // a template chooses the method and the defaults; the member still
              // sees and can edit every field on the configure step
              setForm((cur) => keepNetwork(cur, formFromTemplate(template)));
              setFullForm(false);
              setStep(1);
            }}
          />
          <Card title={t("v2.agents.wizard.methodTitle")} sub={t("v2.agents.wizard.methodSubNative")}>
            <div className="v2-options">
              {METHODS.map((m) => (
                <OptionCard
                  key={m}
                  title={t(`v2.agents.wizard.method.${m}`)}
                  desc={t(`v2.agents.wizard.method.${m}Desc`)}
                  on={method === m}
                  onClick={() => pickMethod(m)}
                  hint={t(m === "harness" ? "glossary.harness" : "glossary.runtime")}
                  badge={m === "harness" ? <Tag tone="blue">{t("v2.agents.wizard.recommended")}</Tag> : undefined}
                  testId={`v2-agent-method-${m}`}
                />
              ))}
              {/* not a form method: the coding console builds the project, then deploys it as byoc */}
              <OptionCard
                title={t("v2.agents.wizard.method.coding")}
                desc={t("v2.agents.wizard.method.codingDesc")}
                on={false}
                onClick={() => navigate("/v2/coding")}
                badge={<Tag tone="outline">{t("v2.agents.wizard.codingBadge")}</Tag>}
                testId="v2-agent-method-coding"
              />
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

      {step === 1 && !edit && (
        <ProjectCard pick={project} error={err("project")} />
      )}

      {step === 1 && method === "harness" && quick && (
        <>
          <Card title={t("v2.agents.wizard.quick.title")} sub={t("v2.agents.wizard.quick.sub")} testId="v2-agent-quick">
            <div className="v2-form">
              <NameField {...section} />
              <Field
                label={t("v2.agents.wizard.quick.promptLabel")}
                required
                error={err("prompt")}
                hint={t("v2.agents.wizard.quick.promptHint")}
              >
                <textarea
                  className="v2-textarea"
                  rows={5}
                  maxLength={20000}
                  value={form.systemPrompt}
                  placeholder={t("v2.agents.wizard.promptPlaceholder")}
                  onChange={(e) => set({ systemPrompt: e.target.value })}
                  data-testid="v2-agent-prompt"
                />
              </Field>
              {kbField(t("create.configure.kbNote"))}
            </div>
            <div className="v2-agents-foot">
              <Button size="sm" onClick={() => setFullForm(true)} testId="v2-agent-quick-full">
                {t("v2.agents.wizard.quick.switchFull")}
              </Button>
            </div>
          </Card>
          <Card title={t("v2.agents.wizard.quick.advanced")} sub={t("v2.agents.wizard.quick.advancedSub")}>
            <Button size="sm" onClick={() => setAdvOpen((v) => !v)} testId="v2-agent-advanced-toggle">
              {t(advOpen || (touched && advancedProblem) ? "v2.agents.wizard.quick.hideAdvanced" : "v2.agents.wizard.quick.showAdvanced")}
            </Button>
          </Card>
          {(advOpen || (touched && advancedProblem)) && (
            <>
              {modelCard({ knobs: true })}
              <HarnessToolsCard {...section} />
              {skillsKb()}
              <MemoryCard {...section} loop />
              <GuardrailCard form={form} set={set} />
              <NetworkSection form={form} set={set} />
            </>
          )}
        </>
      )}

      {step === 1 && method === "harness" && !quick && (
        <>
          {canQuick && (
            <div className="v2-row">
              <Button size="sm" onClick={() => setFullForm(false)} testId="v2-agent-quick-back">
                {t("v2.agents.wizard.quick.switchQuick")}
              </Button>
            </div>
          )}
          <BasicCard {...section} />
          {modelCard({ knobs: true })}
          <HarnessToolsCard {...section} />
          {skillsKb(t("create.configure.kbNote"))}
          {contextCard}
          <FilesystemCard form={form} set={set} touched={touched} />
          <MemoryCard {...section} loop />
          <GuardrailCard form={form} set={set} />
        </>
      )}

      {step === 1 && method === "zip_runtime" && (
        <div className="sb-groups" data-testid="sb-groups">
          {STRANDS_GROUPS.map((group, index) => {
            const flagged = touched && GROUP_PROBLEMS[group].some((key) => problems[key]);
            return (
              <StrandsGroup
                key={group}
                group={group}
                index={index}
                open={openGroups.has(group) || flagged}
                onToggle={() => toggleGroup(group)}
                readout={readouts[group]}
                flagged={flagged}
              >
                {group === "basics" && (
                  <>
                    <BasicCard {...section} />
                    <ProtocolCard form={form} set={set} onProtocol={changeProtocol} />
                    {modelCard(
                      converted
                        ? { showSource: false, sourceNote: t("v2.agents.wizard.convertedModelLocked"), locked: true }
                        : form.protocol === "a2a"
                          ? { showSource: false, sourceNote: t("v2.agents.wizard.a2aSourcePinned") }
                          : {},
                    )}
                  </>
                )}
                {group === "tools" && (
                  <>
                    <StrandsToolsCard {...section} touched={touched} />
                    {(form.protocol === "http" || form.authTools.length > 0) && (
                      <IdentityCard
                        form={form}
                        set={set}
                        touched={touched}
                        connections={connections}
                        catalogError={Boolean(connectionsLoad.error)}
                        onRetry={connectionsLoad.reload}
                      />
                    )}
                    {skillsKb(t("create.configure.kbNoteDirect"))}
                    {contextCard}
                  </>
                )}
                {group === "behaviour" && (
                  <>
                    <ModelParamsCard form={form} set={set} touched={touched} />
                    <PromptTemplateCard form={form} set={set} touched={touched} />
                  </>
                )}
                {group === "output" && (
                  <>
                    <ConversationOutputCard form={form} set={set} touched={touched} />
                    <MemoryCard {...section} loop={false} note={t("create.configure.note")} />
                  </>
                )}
                {group === "advanced" && (
                  <>
                    {inboundCard}
                    <FilesystemCard form={form} set={set} touched={touched} />
                  </>
                )}
              </StrandsGroup>
            );
          })}
        </div>
      )}

      {step === 1 && method === "container" && (
        <>
          <BasicCard {...section} />
          <SdkCard form={form} set={set} />
          {modelCard({ showSource: false, sourceNote: t("v2.agents.wizard.claudeSourcePinned"), claudeOnly: true })}
          <ContainerToolsCard {...section} />
          {skillsKb(t("create.configure.kbNoteDirect"))}
          <FilesystemCard form={form} set={set} touched={touched} />
          {inboundCard}
          <MemoryCard {...section} loop={false} note={t("create.configure.note")} />
        </>
      )}

      {step === 1 && method === "byoc" && form.protocol === "mcp" && (
        <>
          <ByocBasicCard form={form} set={set} err={err} nameLocked={Boolean(edit)} />
          <McpServerCard form={form} set={set} err={err} />
          <ByocArtifactCard
            form={form}
            set={set}
            err={err}
            ui={{ ...ui, customSkills }}
            onUpload={(file) => void uploadZip(file)}
            onPython={changePython}
          />
          <FilesystemCard form={form} set={set} touched={touched} network={false} />
          <ByocEnvCard form={form} set={set} />
        </>
      )}

      {step === 1 && method === "byoc" && form.protocol !== "mcp" && (
        <>
          <ByocBasicCard form={form} set={set} err={err} nameLocked={Boolean(edit)} />
          <ByocArtifactCard
            form={form}
            set={set}
            err={err}
            ui={{ ...ui, customSkills }}
            onUpload={(file) => void uploadZip(file)}
            onPython={changePython}
          />
          <FilesystemCard form={form} set={set} touched={touched} network={false} />
          <ByocModelsCard form={form} set={set} err={err} applySource={applySource} />
          <ByocEnvCard form={form} set={set} />
          <ByocBuiltinsCard form={form} set={set} />
          <IdentityCard
            form={form}
            set={set}
            touched={touched}
            connections={connections}
            catalogError={Boolean(connectionsLoad.error)}
            onRetry={connectionsLoad.reload}
          />
          {inboundCard}
        </>
      )}

      {step === 2 && (
        <WizardReview
          form={form}
          cat={cat}
          ui={{ ...ui, customSkills }}
          project={edit ? null : project.label}
          inlineKb={kb.inline && form.selectedKbs.includes(kb.inline.kb_id) ? kb.inline : null}
          shortTermOff={(edit?.spec as { memory?: { short_term?: boolean } } | undefined)?.memory?.short_term === false}
          workspaceDefault={inboundDefault.data?.default ?? null}
          cognitoIssuer={inboundDefault.data?.cognito_issuer ?? null}
        />
      )}
    </>
  );
}

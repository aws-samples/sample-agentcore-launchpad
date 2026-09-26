/**
 * The agent creation wizard's pure form model — shared by the classic wizard
 * (`pages/CreateAgent.tsx`) and the native V2 wizard (`v2/pages/agents/`), so both
 * consoles post the exact same `AgentSpecInput` for the same inputs and apply the
 * same per-method validation. Nothing here renders or talks to the network.
 */
import type {
  AgentSdk,
  AgentSpecInput,
  ByocArtifactKind,
  ByocPythonVersion,
  HarnessNativeTool,
  Toolkit,
} from "./api";
import type { ModelSource } from "./models";
import { CLAUDE_SDK_MODEL_SOURCE, DEFAULT_MODEL_SOURCE, defaultModelFor } from "./models";
import { DEFAULT_TIMEOUT_SECONDS } from "./agent-defaults";
import {
  DEFAULT_MAX_ITERATIONS,
  EFFORT_NONE,
  effectiveEffort,
  intOrNull,
} from "../pages/create/presetSettings";
import type { EffortChoice } from "../pages/create/presetSettings";

export type AgentMethod = "harness" | "zip_runtime" | "container" | "byoc";

/** Same rule as the backend's `AgentSpec.name`. */
export const AGENT_NAME_RE = /^[a-z][a-z0-9-]{2,47}$/;

export const BUILTIN_TOOLS = ["code-interpreter", "browser"] as const;

/** A multi-select toggle: add `value` if absent, remove it if present. */
export function toggle<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

// Platform toolkits selectable for the Strands ZIP method. `tools` lists the tool
// names the backend will emit — kept here only so the chips can show the resulting
// tool surface without a round-trip; the backend registry
// (backend/app/templates/toolkits/) stays the source of truth for what is emitted.
// `prompt` is the wizard-offered default system prompt: deliberately generic, so an
// agent built from it has prompt-fixable defects a config-bundle A/B can repair.
export const TOOLKITS: {
  name: Toolkit;
  tools: string[];
  prompt: string;
}[] = [
  {
    name: "hr_assistant",
    tools: [
      "get_pto_balance",
      "submit_pto_request",
      "lookup_hr_policy",
      "get_benefits_summary",
      "get_pay_stub",
    ],
    prompt: `You are a helpful HR Assistant for Acme Corp.

You help employees with:
- Checking PTO (paid time off) balances
- Submitting PTO requests
- Looking up HR policies (PTO, remote work, parental leave, code of conduct)
- Understanding employee benefits (health, dental, vision, 401k, life insurance)
- Retrieving pay stub information

Always use the available tools to answer questions accurately. Do not make up
policy details, benefit amounts, or pay information — look them up.
Be concise, professional, and friendly.`,
  },
];

export const TOOLKIT_PROMPTS = TOOLKITS.map((kit) => kit.prompt);

/** Tool names the selected toolkits contribute. Non-empty ⇒ they replace the
 *  template's own calculator/current_utc_time, matching what the backend emits. */
export const toolkitToolNames = (toolkits: Toolkit[]) =>
  TOOLKITS.filter((kit) => toolkits.includes(kit.name)).flatMap((kit) => kit.tools);

/** The prompt after turning a toolkit ON: its default prompt is offered, but the
 *  user's own text is never clobbered — only an empty box or another toolkit's
 *  untouched default is replaced. */
export const promptWithToolkit = (prev: string, kitPrompt: string) =>
  !prev.trim() || TOOLKIT_PROMPTS.includes(prev) ? kitPrompt : prev;

// AgentCore mount-path contract: exactly one level under /mnt
export const MOUNT_RE = /^\/mnt\/[a-zA-Z0-9._-]+$/;
export const DEFAULT_SESSION_MOUNT = "/mnt/workspace";
/** AgentCore allows at most two BYO mounts of each kind */
export const MAX_MOUNTS_PER_KIND = 2;

export const splitIds = (s: string) => s.split(/[\s,]+/).filter(Boolean);
export const skillNameFromPath = (path: string) =>
  path.replace(/\/+$/, "").split("/").pop() ?? path;

// Which source a method starts on. Every method now starts on native Bedrock
// (global inference profiles, Converse); harness and zip/Strands can still be
// switched to Mantle. The container method is pinned — the Claude Agent SDK
// cannot drive anything but Claude, so its model default is Claude-only too
// (`defaultModelForMethod`).
export const MODEL_SOURCE_BY_METHOD: Record<AgentMethod, ModelSource> = {
  harness: DEFAULT_MODEL_SOURCE,
  container: CLAUDE_SDK_MODEL_SOURCE,
  zip_runtime: DEFAULT_MODEL_SOURCE,
  // BYOC deploys the member's own code, but spec.model_id still scopes the
  // execution role's bedrock:InvokeModel and reaches the runtime as env MODEL_ID.
  byoc: DEFAULT_MODEL_SOURCE,
};

// A2A zip agents render from a different template that has no Mantle branch, so
// they stay on the Converse path regardless of the method default.
export const A2A_MODEL_SOURCE: ModelSource = "bedrock";

export const sourceForMethod = (m: AgentMethod): ModelSource => MODEL_SOURCE_BY_METHOD[m];

/** The model a form seeds for `method` on `source` — Claude-only for the Claude Agent SDK. */
export const defaultModelForMethod = (method: AgentMethod, source: ModelSource = sourceForMethod(method)) =>
  defaultModelFor(source, method === "container");

/** The source a method switch lands on: protocol survives a method switch, so
 *  re-entering zip_runtime with A2A still selected lands back on the pinned source. */
export const sourceOnMethodSwitch = (next: AgentMethod, protocol: "http" | "a2a") =>
  next === "zip_runtime" && protocol === "a2a" ? A2A_MODEL_SOURCE : sourceForMethod(next);

// byoc allowed-models cap — mirrors `BYOC_ALLOWED_MODELS_MAX` (backend
// `app/schemas/agent.py`); the bound is IAM policy size, not the catalog.
export const BYOC_MODELS_MAX = 20;

export const BYOC_KINDS: ByocArtifactKind[] = ["code_zip", "container_source", "container_image"];
export const BYOC_PYTHON_VERSIONS: ByocPythonVersion[] = [
  "PYTHON_3_13",
  "PYTHON_3_12",
  "PYTHON_3_11",
  "PYTHON_3_10",
];
export const pythonLabel = (v: ByocPythonVersion) =>
  v.replace("PYTHON_", "Python ").replace("_", ".");
export const BYOC_DEFAULT_ENTRYPOINT = "main.py";

/** After a zip is staged: keep the entrypoint if the scan found it, else take the
 *  first candidate (no candidates ⇒ keep what the member typed). */
export const entrypointAfterUpload = (candidates: string[], current: string) =>
  candidates.length && !candidates.includes(current) ? candidates[0] : current;

// The single member of the "Other Agent SDK" category (the container method).
// Selected by default and, for now, the only selectable value.
export const DEFAULT_AGENT_SDK: AgentSdk = "claude_agent_sdk";

export interface MountRow {
  arn: string;
  path: string;
}

// agent-card skill editor row; tags edit as a comma-separated string
export interface A2aSkillRow {
  name: string;
  description: string;
  tags: string;
}

export interface EnvRow {
  key: string;
  value: string;
}

// the two demo tools every zip template ships — seed the skills editor
export const A2A_SKILL_SEEDS: A2aSkillRow[] = [
  { name: "calculator", description: "Evaluate a basic arithmetic expression", tags: "math" },
  { name: "current time", description: "Report the current UTC date and time", tags: "time" },
];

// backend A2ASkill.id pattern is ^[a-z][a-z0-9_-]{0,63}$ — leading letter required
export const skillSlug = (name: string) =>
  name
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^[^a-z]+/, "")
    .replace(/-+$/, "")
    .slice(0, 64) || "skill";

// rows edit by name; ids must be unique in the spec → suffix repeats (faq, faq-2, …)
export const skillIds = (rows: A2aSkillRow[]): string[] => {
  const used = new Set<string>();
  return rows.map((row) => {
    const base = skillSlug(row.name);
    let id = base;
    for (let n = 2; used.has(id); n += 1) id = `${base.slice(0, 60)}-${n}`;
    used.add(id);
    return id;
  });
};

// APPROVED registry records the wizard offers for mounting.
export interface AttachableMcp {
  name: string;
  description: string;
  url: string;
  gateway: boolean;
  record_id: string;
  gateway_id: string | null;
  gateway_arn: string | null;
  attachable: boolean;
  attachability_reason: string | null;
  auth_type: "aws_iam" | "none" | "oauth" | null;
}
export interface AttachableSkill {
  name: string;
  description: string;
  path: string;
}
// A managed KB offered by the catalog (only ACTIVE + MANAGED are selectable).
export interface AttachableKb {
  kb_id: string;
  name: string;
  description?: string;
  status?: string;
  type?: string;
}
// The redundant KB reference stored in the agent spec (name/description carried
// so the wizard can still render a chip if the KB later leaves the catalog).
export interface KbRef {
  kb_id: string;
  name: string;
  description: string;
}

/** Only ACTIVE managed KBs are selectable; the catalog may already exclude
 *  non-managed KBs, so the type guard is defensive. */
export const selectableKbs = <T extends AttachableKb>(catalog: T[]) =>
  catalog.filter((k) => k.status === "ACTIVE" && (k.type == null || k.type === "MANAGED"));

/** Resolve a KB id to its name/description, preferring the live catalog and
 *  falling back to the loaded spec so out-of-catalog KBs keep their label. */
export const resolveKb = (id: string, catalog: AttachableKb[], specKbs: KbRef[]): KbRef => {
  const cat = catalog.find((k) => k.kb_id === id);
  if (cat) return { kb_id: id, name: cat.name, description: cat.description ?? "" };
  const stored = specKbs.find((k) => k.kb_id === id);
  return { kb_id: id, name: stored?.name ?? id, description: stored?.description ?? "" };
};

export type StoredGatewayConfig = Record<string, { record_id: string; gateway_id: string }>;

/** Everything the member can set in the configure step, for every method. Fields a
 *  method does not use are carried (a method switch keeps them) but never sent. */
export interface AgentForm {
  method: AgentMethod;
  name: string;
  modelId: string;
  modelSource: ModelSource;
  agentSdk: AgentSdk;
  systemPrompt: string;
  // harness-only inference knobs; free text so a half-typed number is validated
  maxTokens: string;
  reasoningEffort: EffortChoice;
  maxIterations: string;
  timeoutSeconds: string;
  /** harness builtin tools */
  tools: string[];
  /** zip_runtime (HTTP) platform toolkits */
  toolkits: Toolkit[];
  selectedGateway: string[];
  selectedMcp: string[];
  selectedKbs: string[];
  skills: string[];
  /** harness expert override; null ⇒ derived from attachments / Skills / native tools */
  allowedTools: string[] | null;
  nativeTools: HarnessNativeTool[];
  longTerm: boolean;
  /** "" ⇒ the workspace's shared default memory */
  memoryId: string;
  /** container: LAUNCHPAD_MCP_SERVERS JSON */
  mcpServers: string;
  // container filesystem configuration
  sessionFs: boolean;
  sessionMount: string;
  s3Mounts: MountRow[];
  efsMounts: MountRow[];
  vpcSubnets: string;
  vpcSgs: string;
  // zip_runtime service protocol
  protocol: "http" | "a2a";
  a2aSkills: A2aSkillRow[];
  // byoc
  byocKind: ByocArtifactKind;
  /** the staged upload (code_zip / container_source); null until one is staged */
  byocUploadId: string | null;
  byocImageUri: string;
  byocEntrypoint: string;
  byocPython: ByocPythonVersion;
  byocInstallReqs: boolean;
  byocRawContract: boolean;
  /** every model the execution role will permit; [0] is the PRIMARY (= model_id) */
  byocModels: string[];
  byocEnvRows: EnvRow[];
  byocDescription: string;
}

/** The fresh form a new agent starts on (the classic wizard's `resetForm`). */
export const emptyAgentForm = (method: AgentMethod = "harness"): AgentForm => ({
  method,
  name: "",
  modelId: defaultModelForMethod(method),
  modelSource: sourceForMethod(method),
  agentSdk: DEFAULT_AGENT_SDK,
  systemPrompt: "",
  maxTokens: "",
  reasoningEffort: EFFORT_NONE,
  maxIterations: String(DEFAULT_MAX_ITERATIONS),
  timeoutSeconds: String(DEFAULT_TIMEOUT_SECONDS),
  tools: [],
  toolkits: [],
  selectedGateway: [],
  selectedMcp: [],
  selectedKbs: [],
  skills: [],
  allowedTools: null,
  nativeTools: [],
  longTerm: true,
  memoryId: "",
  mcpServers: "",
  sessionFs: true,
  sessionMount: DEFAULT_SESSION_MOUNT,
  s3Mounts: [],
  efsMounts: [],
  vpcSubnets: "",
  vpcSgs: "",
  protocol: "http",
  a2aSkills: [],
  byocKind: "code_zip",
  byocUploadId: null,
  byocImageUri: "",
  byocEntrypoint: BYOC_DEFAULT_ENTRYPOINT,
  byocPython: "PYTHON_3_13",
  byocInstallReqs: true,
  byocRawContract: false,
  byocModels: [defaultModelFor(sourceForMethod(method))],
  byocEnvRows: [],
  byocDescription: "",
});

/** What the builders need from the live catalogs. */
export interface AgentFormCatalogs {
  gatewayTargets: AttachableMcp[];
  remoteMcp: AttachableMcp[];
  /** gateway configs carried by a loaded spec (re-publish of a gone record) */
  storedGatewayConfig: StoredGatewayConfig;
  kbInfo: (id: string) => KbRef;
}

// Gateway attachments as ToolRefs. Shared by the harness and zip_runtime
// branches below: the harness service performs the token exchange declaratively,
// a generated runtime does it in code, but the spec shape is the same one.
export const gatewayToolRefs = (form: AgentForm, cat: AgentFormCatalogs) =>
  form.selectedGateway.map((n) => {
    const server = cat.gatewayTargets.find((item) => item.name === n);
    const config =
      server?.record_id && server.gateway_id
        ? { record_id: server.record_id, gateway_id: server.gateway_id }
        : cat.storedGatewayConfig[n];
    return { type: "gateway", name: n, ...(config ? { config } : {}) };
  });

const mcpToolRefs = (form: AgentForm, cat: AgentFormCatalogs) =>
  form.selectedMcp.flatMap((n) => {
    const server = cat.remoteMcp.find((m) => m.name === n);
    return server ? [{ type: "mcp", name: n, config: { url: server.url } }] : [];
  });

export const byocEnv = (rows: EnvRow[]) =>
  Object.fromEntries(
    rows.map((row) => [row.key.trim(), row.value] as const).filter(([k]) => k.length > 0),
  );

export const byocModelList = (models: string[]) => models.map((m) => m.trim()).filter(Boolean);

export const hasByoMounts = (form: Pick<AgentForm, "s3Mounts" | "efsMounts">) =>
  form.s3Mounts.length > 0 || form.efsMounts.length > 0;

export function buildByocSpec(form: AgentForm): AgentSpecInput {
  const env = byocEnv(form.byocEnvRows);
  const models = byocModelList(form.byocModels);
  const kind = form.byocKind;
  return {
    name: form.name,
    method: "byoc",
    // the execution role scopes bedrock:InvokeModel to exactly the allowed-models
    // list; entry [0] is the primary the backend injects as env MODEL_ID (the whole
    // list goes in as ALLOWED_MODEL_IDS; user env rows win for both)
    model_id: models[0],
    model_source: form.modelSource,
    // spec.system_prompt is optional for byoc; the field doubles as a description
    system_prompt: form.byocDescription,
    memory: { short_term: true, long_term: false },
    ...(Object.keys(env).length ? { env } : {}),
    byoc: {
      artifact_kind: kind,
      ...(kind === "container_image"
        ? { image_uri: form.byocImageUri.trim() }
        : { upload_id: form.byocUploadId ?? "" }),
      ...(kind === "code_zip"
        ? {
            entrypoint: form.byocEntrypoint.trim() || BYOC_DEFAULT_ENTRYPOINT,
            python_version: form.byocPython,
            install_requirements: form.byocInstallReqs,
          }
        : {}),
      invoke_contract: form.byocRawContract ? "raw" : "launchpad_prompt",
      allowed_models: models,
    },
  };
}

export function buildOrdinarySpec(form: AgentForm, cat: AgentFormCatalogs): AgentSpecInput {
  const { method, maxTokens, maxIterations, timeoutSeconds, protocol, skills } = form;
  // the reasoning effort the ordinary harness form would send (null ⇒ omitted)
  const ordinaryEffort = effectiveEffort({
    model_id: form.modelId,
    model_source: form.modelSource,
    reasoning_effort: form.reasoningEffort,
  });
  return {
    name: form.name,
    method,
    model_id: form.modelId.trim(), // a pasted custom id may carry stray whitespace
    model_source: form.modelSource,
    // container only — the other methods have no SDK choice to express
    ...(method === "container" ? { agent_sdk: form.agentSdk } : {}),
    // harness-only inference knobs: sent only when set (and, for the effort, only
    // for a model/source pairing the backend accepts) — never for the other methods,
    // whose schema refuses them
    ...(method === "harness" && intOrNull(maxTokens)
      ? { max_tokens: intOrNull(maxTokens) as number }
      : {}),
    ...(method === "harness" && ordinaryEffort ? { reasoning_effort: ordinaryEffort } : {}),
    // loop bounds round-trip as stored (the form starts on the backend defaults)
    ...(intOrNull(maxIterations) ? { max_iterations: intOrNull(maxIterations) as number } : {}),
    ...(intOrNull(timeoutSeconds) ? { timeout_seconds: intOrNull(timeoutSeconds) as number } : {}),
    system_prompt: form.systemPrompt,
    tools:
      method === "harness"
        ? [
            ...form.tools.map((n) => ({ type: "builtin", name: n })),
            ...gatewayToolRefs(form, cat),
            ...mcpToolRefs(form, cat),
          ]
        : method === "container"
          ? mcpToolRefs(form, cat)
          : // An HTTP zip runtime calls the shared Gateway from generated client
            // code; the A2A template carries no MCP client.
            method === "zip_runtime" && protocol === "http"
            ? gatewayToolRefs(form, cat)
            : [],
    memory: {
      short_term: true,
      long_term: form.longTerm,
      ...(form.memoryId ? { memory_id: form.memoryId } : {}),
    },
    ...(method === "zip_runtime"
      ? {
          protocol,
          ...(protocol === "a2a"
            ? {
                a2a_skills: (() => {
                  const rows = form.a2aSkills.filter((s) => s.name.trim());
                  const ids = skillIds(rows);
                  return rows.map((s, i) => ({
                    id: ids[i],
                    name: s.name.trim(),
                    description: s.description.trim(),
                    tags: s.tags.split(",").map((x) => x.trim()).filter(Boolean),
                  }));
                })(),
              }
            : {}),
        }
      : {}),
    // zip_runtime only, and never together with A2A — the backend rejects both.
    ...(method === "zip_runtime" && protocol === "http" && form.toolkits.length
      ? { toolkits: form.toolkits }
      : {}),
    ...(form.selectedKbs.length ? { knowledge_bases: form.selectedKbs.map(cat.kbInfo) } : {}),
    ...((method === "harness" || method === "container" || method === "zip_runtime") &&
    skills.length
      ? { skills }
      : {}),
    ...(method === "harness"
      ? { allowed_tools: form.allowedTools, native_tools: form.nativeTools }
      : {}),
    ...(method === "container" && form.mcpServers.trim()
      ? { env: { LAUNCHPAD_MCP_SERVERS: form.mcpServers.trim() } }
      : {}),
    ...(method === "container"
      ? {
          filesystem: {
            session_storage: form.sessionFs ? { mount_path: form.sessionMount } : null,
            s3_files: form.s3Mounts.map((m) => ({ access_point_arn: m.arn, mount_path: m.path })),
            efs: form.efsMounts.map((m) => ({ access_point_arn: m.arn, mount_path: m.path })),
          },
          ...(hasByoMounts(form)
            ? {
                network: {
                  subnets: splitIds(form.vpcSubnets),
                  security_groups: splitIds(form.vpcSgs),
                },
              }
            : {}),
        }
      : {}),
  } as AgentSpecInput;
}

/** The spec a create / re-publish posts for the form. */
export const buildAgentSpec = (form: AgentForm, cat: AgentFormCatalogs): AgentSpecInput =>
  form.method === "byoc" ? buildByocSpec(form) : buildOrdinarySpec(form, cat);

/* ── validation ─────────────────────────────────────────────────────────── */

/** Container filesystem problems, per input (all false/empty ⇒ valid). */
export interface FilesystemIssues {
  sessionMount: boolean;
  /** `s3:<i>` / `efs:<i>` → which half of the row is invalid */
  rows: Record<string, { arn: boolean; path: boolean }>;
  duplicatePaths: boolean;
  vpc: boolean;
}

export function filesystemIssues(form: AgentForm): FilesystemIssues {
  const paths = [
    ...(form.sessionFs ? [form.sessionMount] : []),
    ...form.s3Mounts.map((m) => m.path),
    ...form.efsMounts.map((m) => m.path),
  ];
  const rows: FilesystemIssues["rows"] = {};
  for (const [kind, list] of [["s3", form.s3Mounts], ["efs", form.efsMounts]] as const) {
    list.forEach((m, i) => {
      const arn = m.arn.trim().length === 0;
      const path = !MOUNT_RE.test(m.path);
      if (arn || path) rows[`${kind}:${i}`] = { arn, path };
    });
  }
  return {
    sessionMount: form.sessionFs && !MOUNT_RE.test(form.sessionMount),
    rows,
    duplicatePaths: new Set(paths).size !== paths.length,
    vpc:
      hasByoMounts(form) &&
      (splitIds(form.vpcSubnets).length === 0 || splitIds(form.vpcSgs).length === 0),
  };
}

export const filesystemValid = (form: AgentForm) => {
  if (form.method !== "container") return true;
  const issues = filesystemIssues(form);
  return (
    !issues.sessionMount &&
    Object.keys(issues.rows).length === 0 &&
    !issues.duplicatePaths &&
    !issues.vpc
  );
};

/** A picked gateway must still be attachable; one that left the catalog is only
 *  kept when the loaded spec carried no config for it. */
export const gatewaySelectionsValid = (form: AgentForm, cat: AgentFormCatalogs) =>
  form.selectedGateway.every((name) => {
    const live = cat.gatewayTargets.find((gateway) => gateway.name === name);
    if (live) return live.attachable;
    return cat.storedGatewayConfig[name] == null;
  });

export const ECR_IMAGE_RE = /\.dkr\.ecr\./;

/** BYOC artifact problems (all false ⇒ valid). */
export interface ByocIssues {
  models: boolean;
  imageUri: boolean;
  upload: boolean;
  entrypoint: boolean;
}

export function byocIssues(form: AgentForm): ByocIssues {
  const image = form.byocKind === "container_image";
  return {
    models: !form.byocModels.some((m) => m.trim().length > 0),
    imageUri: image && !ECR_IMAGE_RE.test(form.byocImageUri.trim()),
    upload: !image && form.byocUploadId == null,
    entrypoint: form.byocKind === "code_zip" && !form.byocEntrypoint.trim().endsWith(".py"),
  };
}

export const byocValid = (form: AgentForm) =>
  form.method !== "byoc" || !Object.values(byocIssues(form)).some(Boolean);

/** The configure step's gate for launch (the classic `configValid`). `knobIssues`
 *  are the harness knob problems (`knobProblems`), empty for the other methods. */
export function agentFormValid(
  form: AgentForm,
  cat: AgentFormCatalogs,
  opts: { knobIssues: string[]; byocUploading: boolean },
): boolean {
  if (form.method === "byoc") {
    return AGENT_NAME_RE.test(form.name) && byocValid(form) && !opts.byocUploading;
  }
  return (
    AGENT_NAME_RE.test(form.name) &&
    form.systemPrompt.trim().length > 0 &&
    // catalog picks are always non-empty; guards a cleared "Custom model ID…" input
    form.modelId.trim().length > 0 &&
    opts.knobIssues.length === 0 &&
    filesystemValid(form) &&
    gatewaySelectionsValid(form, cat)
  );
}

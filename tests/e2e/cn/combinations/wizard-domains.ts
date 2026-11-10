/**
 * Option domains of the two V2 wizards (AgentWizard, TaskWizard) as pairwise factors, the
 * structural constraints that make a row UI-reachable, and the ORACLE: what the wizard must
 * do before submit and what the server must answer for a row.
 *
 * Sources (frontend/src/v2/pages/agents/AgentWizard.tsx + lib/agent-spec.ts,
 * pages/tasks/TaskWizard.tsx; backend/app/schemas/agent.py validators,
 * evaluation/routers.py RunCreate): every rule below cites the line it mirrors. The
 * capability matrix (plans/CAPABILITY-MATRIX-DESIGN.md, cn/r16-a) is the backend twin: its
 * declaration is frozen in `backend/tests/fixtures/method_capabilities.json`, and
 * `ORACLE_METHOD_SETS` below must equal it (wizard-domains.unit.test.ts loads the fixture), so
 * the oracle cannot drift from `MethodCapabilities`. The live suite additionally re-reads
 * `GET /api/tools` builtin `methods` (N15-obs16) at run time; reading
 * `GET /api/capabilities.methods` live is round-17 work.
 *
 * "na" is the value of a factor the method / mode does not expose (the wizard never shows
 * it), so a pair involving it costs nothing and the generator never asks for e.g. a
 * protocol on a harness agent.
 */
import type { Combo, Constraint, Factor } from "./pairwise";

/* ───────────────────────────── AgentWizard ───────────────────────────── */

export type AgentMethod = "harness" | "zip_runtime" | "byoc";

/** What the live catalog offers — fixed per run so the suite is reproducible. */
export interface AgentEnv {
  /** gateway target names the Registry offers (`GET /api/registry/attachables`.mcp_servers gateway=true) */
  gateways: string[];
  /** remote (non-gateway) MCP record names */
  remoteMcp: string[];
  /** APPROVED A2A peers */
  a2aPeers: string[];
  /** `GET /api/tools` builtin rows → methods that render them (N15-obs16) */
  builtinMethods: Record<string, string[]>;
  /** a catalog model whose provider is not configured (rendered disabled) */
  unavailableModel: string | null;
}

export const AGENT_FACTORS = [
  "method",
  "path",
  "builtin",
  "attach",
  "identity",
  "protocol",
  "memory",
  "native",
  "model",
  "maxTokens",
  "maxIter",
  "timeout",
  "toolkit",
  "a2aSkills",
  "fs",
  "byocKind",
  "byocArtifact",
  "byocModels",
] as const;
export type AgentFactorName = (typeof AGENT_FACTORS)[number];

export function agentFactors(env: AgentEnv): Factor[] {
  const attach = ["none", ...(env.gateways.length ? ["gateway"] : []), ...(env.gateways.length > 1 ? ["gateway2"] : []), ...(env.remoteMcp.length ? ["mcp"] : []), ...(env.a2aPeers.length ? ["a2a"] : [])];
  return [
    { name: "method", values: ["harness", "zip_runtime", "byoc"] },
    // detour = pick another method first, then the target (state carry-over across the switch)
    { name: "path", values: ["direct", "detour"] },
    { name: "builtin", values: ["na", "none", "code-interpreter", "browser", "both"] },
    { name: "attach", values: ["na", ...attach] },
    // Identity rows (zip http / byoc): an open REST tool, an MCP tool row without a
    // Connection (a row naming an UNKNOWN Connection is unreachable — the picker lists the
    // workspace's Connections only — and lives in AGENT_API_PROBES)
    { name: "identity", values: ["na", "none", "rest-open", "mcp-noconn"] },
    // http-after-a2a = pick A2A first, then back to HTTP (the model-source pin must lift)
    { name: "protocol", values: ["na", "http", "a2a", "http-after-a2a"] },
    { name: "memory", values: ["na", "lt-on", "lt-off"] },
    { name: "native", values: ["na", "default", "none", "shell"] },
    { name: "model", values: ["default", "custom-bogus", ...(env.unavailableModel ? ["unavailable"] : [])] },
    // one out-of-range value per knob keeps the pairwise set mostly VALID rows (interaction
    // bugs live there); the other invalid shapes (0, abc, 9) are single-control cases (r16-m)
    { name: "maxTokens", values: ["na", "empty", "1", "131072", "131073"] },
    { name: "maxIter", values: ["na", "1", "100", "101"] },
    { name: "timeout", values: ["na", "10", "3600", "3601"] },
    { name: "toolkit", values: ["na", "none", "hr_assistant"] },
    { name: "a2aSkills", values: ["na", "seeds", "plus-empty-row", "none"] },
    { name: "fs", values: ["na", "on", "off", "bad-mount"] },
    { name: "byocKind", values: ["na", "code_zip", "container_source", "container_image"] },
    // code_zip / container_source: nothing staged, or the sample zip; container_image: an
    // in-account-shaped ECR URI, or a URI that is not ECR at all
    { name: "byocArtifact", values: ["na", "none", "sample-zip", "ecr-in-account", "not-ecr"] },
    // the list never empties through the UI (delete shows only with > 1 row) — the empty list
    // is an API probe
    { name: "byocModels", values: ["na", "default", "two"] },
  ];
}

/** `true` while a rule cannot be decided yet (its inputs are not in the partial row). */
type Maybe = boolean | undefined;
/** factor ⇔ condition: present exactly when `cond`; undecidable partials pass (monotone). */
const iff = (p: Combo, name: string, cond: Maybe): boolean => {
  if (p[name] === undefined || cond === undefined) return true;
  return (p[name] === "na") === !cond;
};
const isHttp = (proto: string | undefined): Maybe => (proto === undefined ? undefined : proto !== "a2a");

/** Structural constraint: a factor is "na" exactly when the wizard does not show it. */
export const agentConstraint: Constraint = (p) => {
  const m = p.method as AgentMethod | undefined;
  const harness: Maybe = m === undefined ? undefined : m === "harness";
  const zip: Maybe = m === undefined ? undefined : m === "zip_runtime";
  const byoc: Maybe = m === undefined ? undefined : m === "byoc";
  const http = isHttp(p.protocol);
  const zipHttp: Maybe = zip === undefined ? undefined : !zip ? false : http;
  const zipA2a: Maybe = zip === undefined ? undefined : !zip ? false : http === undefined ? undefined : !http;
  const notByoc: Maybe = byoc === undefined ? undefined : !byoc;
  const toolsOn: Maybe = harness === undefined ? undefined : harness ? true : zipHttp; // harness, or zip over HTTP
  const identityOn: Maybe = byoc === undefined ? undefined : byoc ? true : zipHttp;
  if (!iff(p, "builtin", harness)) return false;
  if (!iff(p, "native", harness)) return false;
  if (!iff(p, "maxTokens", harness)) return false;
  if (!iff(p, "maxIter", harness)) return false;
  if (!iff(p, "timeout", harness)) return false;
  if (!iff(p, "protocol", zip)) return false;
  if (!iff(p, "memory", notByoc)) return false;
  if (!iff(p, "fs", notByoc)) return false;
  if (!iff(p, "byocKind", byoc)) return false;
  if (!iff(p, "byocArtifact", byoc)) return false;
  if (!iff(p, "byocModels", byoc)) return false;
  if (!iff(p, "attach", toolsOn)) return false;
  if (!iff(p, "identity", identityOn)) return false;
  if (!iff(p, "toolkit", zipHttp)) return false;
  if (!iff(p, "a2aSkills", zipA2a)) return false;
  // the artifact value must match the byoc kind
  if (p.byocKind !== undefined && p.byocArtifact !== undefined && p.byocKind !== "na" && p.byocArtifact !== "na") {
    const image = p.byocKind === "container_image";
    if (image !== ["ecr-in-account", "not-ecr"].includes(p.byocArtifact)) return false;
  }
  return true;
};

/** What the oracle assumes each method accepts — the projection of the backend's
 *  `MethodCapabilities` declaration the rules in `expectAgent` / `AGENT_API_PROBES` rely on.
 *  Kept equal to `backend/tests/fixtures/method_capabilities.json` by the unit test. */
export const ORACLE_METHOD_SETS: Record<AgentMethod, { builtin_tools: string[]; tool_types: string[]; protocols: string[]; byoc_kinds: string[]; knowledge_bases: boolean; native_tools: string[] }> = {
  // harness renders both builtins (r15-h), takes catalog attachments, never rest tools / auth, HTTP only
  harness: { builtin_tools: ["browser", "code-interpreter"], tool_types: ["a2a", "builtin", "gateway", "mcp"], protocols: ["http"], byoc_kinds: [], knowledge_bases: true, native_tools: ["file_operations", "shell"] },
  // the Strands zip template: gateway / a2a / rest tools over HTTP, A2A server mode, KBs, no builtins
  zip_runtime: { builtin_tools: [], tool_types: ["a2a", "gateway", "rest"], protocols: ["a2a", "http"], byoc_kinds: [], knowledge_bases: true, native_tools: [] },
  // byoc: the member's code owns tools / KBs; three artifact kinds; HTTP only
  byoc: { builtin_tools: [], tool_types: [], protocols: ["http"], byoc_kinds: ["code_zip", "container_image", "container_source"], knowledge_bases: false, native_tools: [] },
};

export type Verdict = "blocked" | "allowed";

export interface AgentExpectation {
  /** what the wizard must do before submit */
  ui: Verdict;
  /** why the wizard blocks (the controls that must show an error / be disabled) */
  uiReasons: string[];
  /** what `POST /api/agents` must answer for the spec the wizard would send:
   *  "accept" (2xx, or 409 agent.name_reserved on a reserved-name replay) or a 4xx */
  server: "accept" | 422 | 404 | 409;
  serverReasons: string[];
}

const OUT_OF_RANGE_TOKENS = new Set(["131073", "0", "abc"]);
const OUT_OF_RANGE_ITER = new Set(["0", "101"]);
const OUT_OF_RANGE_TIMEOUT = new Set(["9", "3601"]);

/** The oracle. Line references: W = AgentWizard.tsx, S = lib/agent-spec.ts, B = schemas/agent.py. */
export function expectAgent(c: Combo, env: AgentEnv): AgentExpectation {
  const uiReasons: string[] = [];
  const serverReasons: string[] = [];
  const m = c.method as AgentMethod;
  const offered = (name: string) => {
    const methods = env.builtinMethods[name];
    return methods === undefined || methods.includes(m);
  };
  if (m === "harness") {
    // W:258-266 knob ranges mirror B:1076/1117/1118
    if (OUT_OF_RANGE_TOKENS.has(c.maxTokens)) {
      uiReasons.push("maxTokens");
      serverReasons.push("max_tokens 1..131072");
    }
    if (OUT_OF_RANGE_ITER.has(c.maxIter)) {
      uiReasons.push("maxIterations");
      serverReasons.push("max_iterations 1..100");
    }
    if (OUT_OF_RANGE_TIMEOUT.has(c.timeout)) {
      uiReasons.push("timeout");
      serverReasons.push("timeout_seconds 10..3600");
    }
    // N15-obs16: a builtin the catalog does not list for the method is disabled (W:HarnessToolsCard)
    for (const b of c.builtin === "both" ? ["code-interpreter", "browser"] : c.builtin === "none" || c.builtin === "na" ? [] : [c.builtin]) {
      if (!offered(b)) {
        uiReasons.push(`builtin:${b}:disabled`);
        serverReasons.push("agent.builtin_unsupported");
      }
    }
  }
  if (m !== "byoc" && c.fs === "bad-mount") {
    uiReasons.push("fs.sessionMount"); // S:915 MOUNT_RE
    serverReasons.push("filesystem mount path");
  }
  if (c.identity === "mcp-noconn") {
    uiReasons.push("identity.connection:required"); // S:1018 (mcp rows need one)
    // the server accepts an mcp tool without auth on zip http — the row is a UI rule only
  }
  if (c.identity === "rest-open" && m === "byoc") {
    uiReasons.push("identity.connection:required"); // S:1018 byoc rows need a Connection
    serverReasons.push("byoc tools must carry auth"); // B:1197
  }
  if (m === "byoc") {
    if (c.byocArtifact === "none") {
      uiReasons.push("byocUpload"); // S:959
      serverReasons.push("upload_id required"); // B:1025
    }
    if (c.byocArtifact === "not-ecr") {
      uiReasons.push("byocImage"); // S:958 ECR_IMAGE_RE
      serverReasons.push("image_uri must be private ECR"); // byoc.py:156 (delayed today)
    }
  }
  if (c.model === "unavailable") {
    uiReasons.push("model:option-disabled"); // wizardKit ModelCard disabled: !o.available
    serverReasons.push("models.provider_not_configured"); // assert_spec_models_available
  }
  if (c.model === "custom-bogus") {
    // the wizard offers a free-text id and lets the server decide (S:1081 non-empty only)
    serverReasons.push("models.model_not_allowed"); // B:_model_catalog
  }
  const ui: Verdict = uiReasons.length ? "blocked" : "allowed";
  const server: AgentExpectation["server"] = serverReasons.length ? 422 : "accept";
  return { ui, uiReasons, server, serverReasons };
}

/** Rows the wizard cannot produce but the API can: the server rules of the capability
 *  matrix the wizard relies on (plans/CAPABILITY-MATRIX-DESIGN.md §0.3). Each probe is a
 *  partial spec merged over a valid base; `expect` is the status `POST /api/agents` must
 *  answer with NO row and NO job. "accept" = passes validation (409 agent.name_reserved on
 *  the reserved-name replay the suite uses so nothing deploys). */
export interface ApiProbe {
  id: string;
  method: AgentMethod;
  patch: Record<string, unknown>;
  expect: "accept" | 422 | 404 | 409;
  rule: string;
}

/** a well-formed (`^[A-Za-z0-9]+$`, ≤ 32) id no KB has */
export const BOGUS_KB = (n: number) => ({ kb_id: `X16NBOGUSKB${n}`, name: `x16n bogus ${n}`, description: "" });

export const AGENT_API_PROBES: ApiProbe[] = [
  { id: "byoc-kb", method: "byoc", patch: { knowledge_bases: [BOGUS_KB(1)] }, expect: 422, rule: "B:1188 knowledge_bases not supported by byoc" },
  { id: "zip-a2a-kb", method: "zip_runtime", patch: { protocol: "a2a", knowledge_bases: [BOGUS_KB(1)], tools: [] }, expect: 422, rule: "B:1455 knowledge_bases not supported by protocol=a2a" },
  { id: "harness-rest", method: "harness", patch: { tools: [{ type: "rest", name: "x16n_rest", config: { url: "https://example.invalid/" }, auth: null }] }, expect: 422, rule: "B:1273 rest tools zip_runtime/byoc only" },
  { id: "zip-max-tokens", method: "zip_runtime", patch: { max_tokens: 100 }, expect: 422, rule: "B:1372 max_tokens harness only" },
  { id: "zip-native", method: "zip_runtime", patch: { native_tools: ["shell"] }, expect: 422, rule: "B:1387 native_tools harness only" },
  { id: "byoc-no-block", method: "byoc", patch: { byoc: null }, expect: 422, rule: "B:1183 method=byoc requires byoc block" },
  { id: "harness-a2a-proto", method: "harness", patch: { protocol: "a2a" }, expect: 422, rule: "B:1313 A2A zip_runtime only" },
  { id: "zip-a2a-toolkit", method: "zip_runtime", patch: { protocol: "a2a", toolkits: ["hr_assistant"], tools: [] }, expect: 422, rule: "B:_toolkits_supported a2a" },
  { id: "harness-mcp-auth", method: "harness", patch: { tools: [{ type: "mcp", name: "x16n_mcp", config: { url: "https://example.invalid/mcp" }, auth: { connection: "x16n-none", kind: "oauth2" } }] }, expect: 422, rule: "B:1273 tool auth zip_runtime/byoc only" },
  { id: "kb-11", method: "harness", patch: { knowledge_bases: Array.from({ length: 11 }, (_, i) => BOGUS_KB(i)) }, expect: 422, rule: "B:1130 knowledge_bases max 10" },
  // KNOWN N16-obs73: a well-formed KB id nothing owns passes creation (check_pins only guards
  // visibility; the existence check joins validate_against_method in cn/r16-a) — replayed under
  // the reserved name so the known gap deploys nothing; "accept" here = the known behaviour
  { id: "kb-bogus-1", method: "harness", patch: { knowledge_bases: [BOGUS_KB(1)] }, expect: "accept", rule: "KNOWN N16-obs73: bogus KB id passes creation (schema-valid, no existence check yet)" },
  { id: "kb-bogus-10", method: "harness", patch: { knowledge_bases: Array.from({ length: 10 }, (_, i) => BOGUS_KB(i)) }, expect: "accept", rule: "KNOWN N16-obs73 at the 10-KB cap" },
  { id: "memory-bogus", method: "harness", patch: { memory: { short_term: true, long_term: true, memory_id: "x16n-bogus-memory" } }, expect: 422, rule: "memory_ownership.require_spec_memory (agent.memory_not_managed)" },
  { id: "model-unavailable", method: "harness", patch: { model_id: "siliconflow.zai-org/GLM-5.3" }, expect: 422, rule: "assert_spec_models_available: provider not configured" },
  { id: "tool-auth-unknown-conn", method: "zip_runtime", patch: { tools: [{ type: "rest", name: "x16n_rest", config: { url: "https://example.invalid/" }, auth: { connection: "x16n-no-such-connection", kind: "oauth2", mode: "as_agent", scopes: [] } }] }, expect: 422, rule: "routers/agents._validate_tool_auth: the Connection must exist in the workspace (real-name probe)" },
  { id: "byoc-models-empty", method: "byoc", patch: { byoc: { artifact_kind: "container_image", image_uri: "placeholder", allowed_models: [] } }, expect: 422, rule: "B:1001 allowed_models entries non-empty (the wizard never empties the list)" },
  { id: "browser-harness", method: "harness", patch: { tools: [{ type: "builtin", name: "browser" }] }, expect: "accept", rule: "GET /api/tools builtin browser.methods includes harness (r15-h)" },
];

/* ───────────────────────────── TaskWizard ───────────────────────────── */

export const TASK_FACTORS = ["target", "source", "mode", "strategy", "window", "sampling", "evaluators", "insights", "dataset", "picked"] as const;

export function taskFactors(): Factor[] {
  return [
    { name: "target", values: ["agent", "cloudwatch"] },
    { name: "source", values: ["window", "sessions", "logs", "dataset"] },
    { name: "mode", values: ["evaluators", "insights"] },
    { name: "strategy", values: ["history", "continuous"] },
    { name: "window", values: ["na", "1", "24", "336"] },
    // 0 / 101 are typed and must clamp to 10 / 100 (TaskWizard.tsx:478)
    { name: "sampling", values: ["na", "0.01", "10", "100", "0", "101"] },
    // none = [] (server 422, UI error); gt = a trajectory evaluator that needs ground truth
    { name: "evaluators", values: ["na", "one", "two", "ten", "none", "gt"] },
    { name: "insights", values: ["na", "all", "one", "none"] },
    { name: "dataset", values: ["na", "gt", "sim"] },
    // sessions / log streams picked: insights needs >= 3 (MIN_INSIGHT_SESSIONS)
    { name: "picked", values: ["na", "one", "three"] },
  ];
}

export const taskConstraint: Constraint = (p) => {
  const cw = p.target === "cloudwatch";
  if (cw && (p.source === "sessions" || p.source === "dataset")) return false; // TaskWizard.tsx:300 disabled cards
  if (cw && p.strategy === "continuous") return false; // :310
  if (p.strategy === "continuous" && p.source !== undefined && p.source !== "window") return false; // :313 / :460
  if (p.strategy === "continuous" && p.mode === "insights") return false; // :315
  const continuous: Maybe = p.strategy === undefined ? undefined : p.strategy === "continuous";
  const windowed: Maybe = p.source === undefined ? undefined : p.source === "window" || p.source === "logs";
  const windowOn: Maybe = windowed === false || continuous === true ? false : windowed === undefined || continuous === undefined ? undefined : true;
  if (!iff(p, "sampling", continuous)) return false;
  if (!iff(p, "window", windowOn)) return false;
  if (!iff(p, "dataset", p.source === undefined ? undefined : p.source === "dataset")) return false;
  if (!iff(p, "picked", p.source === undefined ? undefined : p.source === "sessions" || p.source === "logs")) return false;
  if (!iff(p, "evaluators", p.mode === undefined ? undefined : p.mode === "evaluators")) return false;
  if (!iff(p, "insights", p.mode === undefined ? undefined : p.mode === "insights")) return false;
  return true;
};

export interface TaskExpectation {
  ui: Verdict;
  uiReasons: string[];
  /** "accept" = 201 run / online config; else the status the server must answer */
  server: "accept" | 400 | 409 | 422;
  serverReasons: string[];
  /** the wizard clamps the typed value to this before sending */
  samplingSent?: number;
}

export interface TaskEnv {
  /** sessions of the fixture agent inside each window (hours → count) */
  sessionsInWindow: (hours: number) => number;
  /** the gt dataset's items carry ground truth; the sim one does not */
  gtDatasetHasGroundTruth: boolean;
}

export function expectTask(c: Combo, env: TaskEnv): TaskExpectation {
  const uiReasons: string[] = [];
  const serverReasons: string[] = [];
  let server: TaskExpectation["server"] = "accept";
  if (c.mode === "evaluators" && c.evaluators === "none") {
    uiReasons.push("errEvaluators"); // TaskWizard.tsx:245
    serverReasons.push("evaluators [] (r13-h → 422)");
    server = 422;
  }
  if (c.mode === "insights" && c.insights === "none") {
    uiReasons.push("errInsights"); // :244
    serverReasons.push("insights []");
    server = 422;
  }
  if (c.mode === "insights" && (c.source === "sessions" || c.source === "logs") && c.picked === "one") {
    uiReasons.push("errInsightSessions"); // :230 (< MIN_INSIGHT_SESSIONS)
  }
  if (c.strategy !== "continuous" && c.source === "window" && c.target === "agent") {
    const n = env.sessionsInWindow(Number(c.window));
    if (n === 0) {
      // the preview says "0 sessions" but the wizard still submits — the server answers
      serverReasons.push("run.no_sessions"); // service.py:268 (409)
      if (server === "accept") server = 409;
    }
  }
  if (c.mode === "evaluators" && c.evaluators === "gt" && !(c.source === "dataset" && c.dataset === "gt" && env.gtDatasetHasGroundTruth)) {
    // the wizard only WARNS (gtWarning); the server refuses before any row
    serverReasons.push("ground-truth evaluator without ground truth (_assert_target_references)");
    if (server === "accept") server = 422;
  }
  if (c.source === "dataset" && c.dataset === "sim" && c.mode === "insights") {
    // a persona replay clusters nothing until the actor ran — accepted, observed later
  }
  const samplingSent = c.sampling === "na" ? undefined : c.sampling === "0" ? 10 : c.sampling === "101" ? 100 : Number(c.sampling);
  return { ui: uiReasons.length ? "blocked" : "allowed", uiReasons, server, serverReasons, samplingSent };
}

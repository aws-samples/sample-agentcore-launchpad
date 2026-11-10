/**
 * AgentWizard pairwise (round 16, r16-n step 1). For every all-pairs row of the wizard's
 * option domain (wizard-domains.ts) the suite drives the real wizard and asserts ONE of:
 *   (a) the wizard disables / explains the invalid choice before submit — and the server
 *       refuses the same spec (422, replayed under a reserved name so nothing deploys);
 *   (b) the wizard reaches the review step (which must reflect the choices) and the server
 *       accepts the spec the wizard sends — live for the first X16N_LIVE_CAP rows (the
 *       created row must match the request), replayed under the reserved name for the rest.
 * Any third outcome is recorded as a finding in evidence/results.jsonl.
 *
 * Fixtures: `x16n-*` agents only; the live `x16n-fx-harness` row is the TaskWizard's target
 * (w2). Cleanup is w9-cleanup.spec.ts (or X16N_CLEANUP=1 at the end of this file).
 */
import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

import * as L from "./lib";
import { allPairs, type Combo, comboLabel } from "./pairwise";
import * as dom from "./wizard-domains";

const SEED = Number(process.env.X16N_SEED ?? 16);
const LIVE_CAP = Number(process.env.X16N_LIVE_CAP ?? 6);
const METHODS: dom.AgentMethod[] = ["harness", "zip_runtime", "byoc"];
const PROMPT = "You are x16n, a round-16 combination test agent. Answer in one short sentence.";
const BOGUS_MODEL = "x16n.bogus-model-id";
const SUITE = "wizard-agent";
/** per-invocation tag in generated agent names: a re-run never collides with a kept row */
const TAG = Date.now().toString(36).slice(-4);

/** valid anchor rows prepended to the pairwise set: the live fixtures the task wizard needs */
const ANCHORS: { name: string; combo: Combo }[] = [
  {
    name: "x16n-fx-harness",
    combo: { method: "harness", path: "direct", builtin: "both", attach: "gateway", identity: "na", protocol: "na", memory: "lt-on", native: "default", model: "default", maxTokens: "empty", maxIter: "100", timeout: "3600", toolkit: "na", a2aSkills: "na", fs: "on", byocKind: "na", byocArtifact: "na", byocModels: "na" },
  },
  {
    name: "x16n-fx-zip-http",
    combo: { method: "zip_runtime", path: "detour", builtin: "na", attach: "gateway", identity: "rest-open", protocol: "http", memory: "lt-off", native: "na", model: "default", maxTokens: "na", maxIter: "na", timeout: "na", toolkit: "hr_assistant", a2aSkills: "na", fs: "on", byocKind: "na", byocArtifact: "na", byocModels: "na" },
  },
  {
    name: "x16n-fx-zip-a2a",
    combo: { method: "zip_runtime", path: "direct", builtin: "na", attach: "na", identity: "na", protocol: "a2a", memory: "lt-on", native: "na", model: "default", maxTokens: "na", maxIter: "na", timeout: "na", toolkit: "na", a2aSkills: "seeds", fs: "off", byocKind: "na", byocArtifact: "na", byocModels: "na" },
  },
  {
    name: "x16n-fx-byoc",
    combo: { method: "byoc", path: "detour", builtin: "na", attach: "na", identity: "none", protocol: "na", memory: "na", native: "na", model: "default", maxTokens: "na", maxIter: "na", timeout: "na", toolkit: "na", a2aSkills: "na", fs: "na", byocKind: "code_zip", byocArtifact: "sample-zip", byocModels: "two" },
  },
];

// tests run in file order (workers: 1, fullyParallel: false); NOT serial mode — a soft failure in one test must not skip the rest

let admin: L.Session;
let env: dom.AgentEnv;
let ws: { account_id: string; region: string } = { account_id: "", region: "" };
let zips: { http: string; container: string };
// Playwright restarts the worker after a failed test (soft failures included), which would
// drop module state — so the state the later tests need is persisted under EVIDENCE.
const STATE_FILE = path.join(L.EVIDENCE, "w1-state.json");
interface W1State {
  baseSpecs: Partial<Record<dom.AgentMethod, Record<string, unknown>>>;
  blockedPending: { id: string; combo: Combo; exp: dom.AgentExpectation }[];
  liveAgents: { name: string; id: string }[];
  liveCount: number;
}
const state: W1State = fs.existsSync(STATE_FILE) ? (JSON.parse(fs.readFileSync(STATE_FILE, "utf8")) as W1State) : { baseSpecs: {}, blockedPending: [], liveAgents: [], liveCount: 0 };
const saveState = () => fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
const { baseSpecs, blockedPending, liveAgents } = state;

async function readEnv(api: L.Api): Promise<dom.AgentEnv> {
  const att = (await api.get("/api/registry/attachables")).body ?? {};
  const tools = (await api.get("/api/tools")).body ?? {};
  const caps = (await api.get("/api/capabilities")).body ?? {};
  const toolRows: { name: string; source: string; methods?: string[] }[] = tools.tools ?? tools ?? [];
  const builtinMethods = Object.fromEntries(toolRows.filter((t) => t.source === "builtin").map((t) => [t.name, t.methods ?? []]));
  const unavailable = (caps.models ?? []).find((m: { tools: boolean; available: boolean }) => m.tools && !m.available);
  return {
    gateways: (att.mcp_servers ?? []).filter((m: { gateway: boolean; attachable: boolean }) => m.gateway && m.attachable).map((m: { name: string }) => m.name),
    remoteMcp: (att.mcp_servers ?? []).filter((m: { gateway: boolean; attachable: boolean }) => !m.gateway && m.attachable).map((m: { name: string }) => m.name),
    a2aPeers: (att.agents ?? []).filter((a: { attachable: boolean }) => a.attachable).map((a: { name: string }) => a.name),
    builtinMethods,
    unavailableModel: unavailable?.model_id ?? null,
  };
}

test.beforeAll(async ({ browser }) => {
  admin = await L.session(browser, "admin");
  env = await readEnv(admin.api);
  const w = (await admin.api.get("/api/workspaces")).body?.workspaces?.find((x: { id: string }) => x.id === "default");
  ws = { account_id: w?.account_id ?? "", region: w?.region ?? "" };
  const tmp = fs.mkdtempSync(path.join(process.env.TMPDIR ?? "/tmp", "x16n-byoc-"));
  const samples = path.resolve(__dirname, "../../../../samples/byoc");
  zips = { http: path.join(tmp, "hello-http.zip"), container: path.join(tmp, "hello-container.zip") };
  execFileSync("zip", ["-q", "-r", zips.http, "."], { cwd: path.join(samples, "hello-http") });
  execFileSync("zip", ["-q", "-r", zips.container, "."], { cwd: path.join(samples, "hello-container") });
  const r = allPairs(dom.agentFactors(env), { seed: SEED, constraint: dom.agentConstraint });
  L.writeJson("w1-plan.json", { seed: SEED, env, anchors: ANCHORS, pairs: r.pairCount, fullProduct: r.fullProductSize, combos: r.combos.length, infeasible: r.infeasiblePairs });
  L.log("w1 plan", { combos: r.combos.length, pairs: r.pairCount, product: r.fullProductSize, env });
});

test.afterAll(async () => {
  L.writeJson("w1-live-agents.json", liveAgents);
  if (process.env.X16N_CLEANUP === "1") await L.deleteAgentsWithPrefix(admin.api);
  await admin.ctx.close();
});

/* ───────────── driving ───────────── */
interface Drive {
  uiBlocked: boolean;
  uiReasons: string[];
  notes: string[];
  atReview: boolean;
  reviewText: string;
  screenshots: string[];
}

const ecrHost = () => `${ws.account_id}.dkr.ecr.${ws.region}.${["amazonaws", "com", "cn"].join(".")}`;

async function setChecked(box: ReturnType<Page["locator"]>, on: boolean) {
  if ((await box.isChecked()) !== on) await box.click();
}

async function drive(page: Page, combo: Combo, name: string, id: string): Promise<Drive> {
  const d: Drive = { uiBlocked: false, uiReasons: [], notes: [], atReview: false, reviewText: "", screenshots: [] };
  const method = combo.method as dom.AgentMethod;
  await L.goto(page, "/v2/agents?view=new", 300);
  await page.getByTestId("v2-agent-method-harness").waitFor({ timeout: 20_000 });
  if (combo.path === "detour") {
    const other = METHODS.find((m) => m !== method)!;
    await page.getByTestId(`v2-agent-method-${other}`).click();
    if (other !== "byoc" && method !== "byoc") {
      // state carried across the switch: nothing to set at step 0, the configure step follows
    }
  }
  await page.getByTestId(`v2-agent-method-${method}`).click();
  await page.getByTestId("v2-agent-wizard-next").click();
  await page.getByTestId("v2-agent-name").waitFor();
  await page.getByTestId("v2-agent-name").fill(name);

  if (method !== "byoc") {
    await page.getByTestId("v2-agent-prompt").fill(PROMPT);
  }
  // protocol (zip)
  if (method === "zip_runtime") {
    if (combo.protocol === "a2a" || combo.protocol === "http-after-a2a") await page.getByTestId("v2-agent-protocol-a2a").click();
    if (combo.protocol === "http-after-a2a") {
      await page.getByTestId("v2-agent-protocol-http").click();
      // the A2A pin on the model source must lift: the model id must be the default again
      const modelId = await page.locator('[data-testid="v2-agent-model"]').first().innerText().catch(() => "");
      d.notes.push(`model after a2a→http: ${modelId.trim().slice(0, 60)}`);
    }
  }
  // model (the byoc card has its own picker: an added row, the primary stays the default)
  const modelTrigger = method === "byoc" ? page.getByTestId("v2-agent-byoc-model-add") : page.getByTestId("v2-agent-model");
  if (combo.model === "custom-bogus") {
    await L.pickOption(page, modelTrigger, /Custom model ID/);
    if (method === "byoc") {
      await page.getByTestId("v2-agent-byoc-model-custom").fill(BOGUS_MODEL);
      await page.getByTestId("v2-agent-byoc-model-custom-add").click();
    } else await page.getByTestId("v2-agent-model-custom").fill(BOGUS_MODEL);
  } else if (combo.model === "unavailable" && env.unavailableModel) {
    const opts = await L.listOptions(page, modelTrigger);
    const row = opts.find((o) => o.label.toLowerCase().includes("siliconflow") || o.label.includes(env.unavailableModel ?? "∅"));
    if (row?.disabled) d.uiReasons.push("model:option-disabled");
    else d.notes.push(`unavailable model option not disabled: ${JSON.stringify(row)}`);
  }
  // harness tools / knobs
  if (method === "harness") {
    const builtins = page.locator('[data-testid="v2-agent-builtins"] input[type=checkbox]');
    const want = combo.builtin === "both" ? [0, 1] : combo.builtin === "code-interpreter" ? [0] : combo.builtin === "browser" ? [1] : [];
    for (const i of want) {
      const box = builtins.nth(i);
      if (await box.isDisabled()) d.uiReasons.push(`builtin:${i === 0 ? "code-interpreter" : "browser"}:disabled`);
      else await setChecked(box, true);
    }
    const native = page.locator('[data-testid="v2-agent-native-tools"] input[type=checkbox]');
    if ((await native.count()) === 2) {
      const both = combo.native === "default";
      await setChecked(native.nth(0), both || combo.native === "shell");
      await setChecked(native.nth(1), both);
    } else d.notes.push(`native tools: ${await native.count()} boxes`);
    const tokens = page.getByTestId("v2-agent-max-tokens");
    await tokens.fill(combo.maxTokens === "empty" ? "" : combo.maxTokens);
    await page.getByTestId("v2-agent-max-iterations").fill(combo.maxIter);
    await page.getByTestId("v2-agent-timeout").fill(combo.timeout);
  }
  // attachments (harness / zip http)
  if (combo.attach !== "na" && combo.attach !== "none") {
    const boxes = page.locator('[data-testid="v2-agent-gateways"] input[type=checkbox]');
    const n = await boxes.count();
    const want = combo.attach === "gateway2" ? 2 : 1;
    if (n < want) d.notes.push(`gateways offered ${n} < wanted ${want}`);
    for (let i = 0; i < Math.min(n, want); i += 1) await setChecked(boxes.nth(i), true);
  }
  // toolkit (zip http) — must not clobber the typed prompt
  if (combo.toolkit === "hr_assistant") {
    await page.locator('[data-testid="v2-agent-toolkits"] input[type=checkbox]').first().click();
    const prompt = await page.getByTestId("v2-agent-prompt").inputValue();
    if (prompt !== PROMPT) d.notes.push("toolkit toggle replaced the typed prompt");
  }
  // identity rows (zip http / byoc)
  if (combo.identity === "rest-open" || combo.identity === "mcp-noconn") {
    await page.getByTestId(combo.identity === "rest-open" ? "v2-agent-identity-add-rest" : "v2-agent-identity-add-mcp").click();
    await page.getByTestId("v2-agent-identity-name-0").fill(combo.identity === "rest-open" ? "x16n_rest" : "x16n_mcp");
    const url = page.getByTestId("v2-agent-identity-url-0");
    if (await url.count()) await url.fill("https://example.invalid/x16n");
  }
  // a2a skills
  if (combo.a2aSkills === "plus-empty-row") await page.getByTestId("v2-agent-a2a-add").click();
  if (combo.a2aSkills === "none") {
    const rows = page.locator('[data-testid="v2-agent-a2a-skills"] .v2-agents-row');
    for (let n = await rows.count(); n > 0; n -= 1) await rows.nth(n - 1).locator("button").last().click();
  }
  // memory / filesystem
  if (combo.memory !== "na") await setChecked(page.getByTestId("v2-agent-long-term"), combo.memory === "lt-on");
  if (combo.fs !== "na") {
    await setChecked(page.getByTestId("v2-agent-fs-session"), combo.fs !== "off");
    if (combo.fs === "bad-mount") await page.getByTestId("v2-agent-fs-session-mount").fill("/mnt");
  }
  // byoc
  if (method === "byoc") {
    await page.getByTestId(`v2-agent-byoc-kind-${combo.byocKind}`).click();
    if (combo.byocArtifact === "sample-zip") {
      await page.getByTestId("v2-agent-byoc-file").setInputFiles(combo.byocKind === "container_source" ? zips.container : zips.http);
      const ok = await page.getByTestId("v2-agent-byoc-uploaded").waitFor({ timeout: 150_000 }).then(() => true).catch(() => false);
      if (!ok) d.notes.push("byoc upload did not report a staged zip within 150 s");
    } else if (combo.byocArtifact === "ecr-in-account") {
      await page.getByTestId("v2-agent-byoc-image").fill(`${ecrHost()}/x16n/hello:latest`);
    } else if (combo.byocArtifact === "not-ecr") {
      await page.getByTestId("v2-agent-byoc-image").fill("docker.io/library/x16n:latest");
    }
    if (combo.byocModels === "two") await L.pickOption(page, page.getByTestId("v2-agent-byoc-model-add"), /Kimi|DeepSeek|GLM/);
  }
  d.screenshots.push(await L.shot(page, `${id}-configure`));
  // → review
  await page.getByTestId("v2-agent-wizard-next").click();
  await page.waitForTimeout(600);
  d.atReview = await page.getByTestId("v2-agent-review").isVisible().catch(() => false);
  if (!d.atReview) {
    const text = await L.bodyText(page);
    d.uiBlocked = true;
    if (text.includes("Fix the highlighted problems first.")) d.uiReasons.push("fixErrors-alert");
    const errs = await page.locator(".v2-field-error, .v2-error, [class*=error]").allInnerTexts().catch(() => []);
    d.uiReasons.push(...errs.map((e) => e.trim()).filter(Boolean).slice(0, 6));
    d.screenshots.push(await L.shot(page, `${id}-blocked`));
  } else {
    d.reviewText = await page.getByTestId("v2-agent-review").innerText();
    d.screenshots.push(await L.shot(page, `${id}-review`));
  }
  // a disabled option IS the wizard explaining itself before submit (N15-obs16 pattern)
  if (d.uiReasons.some((r) => /disabled$/.test(r))) d.uiBlocked = true;
  return d;
}

/** The review step must echo the choices (name, method, knobs, protocol, toolkit, memory). */
function reviewGaps(combo: Combo, name: string, text: string): string[] {
  const gaps: string[] = [];
  if (!text.includes(name)) gaps.push("name");
  // numbers may render locale-formatted (131,072)
  const hasNum = (v: string) => text.includes(v) || (/^\d+$/.test(v) && text.includes(Number(v).toLocaleString("en-US")));
  if (combo.method === "harness") {
    if (combo.maxTokens !== "empty" && !hasNum(combo.maxTokens)) gaps.push(`maxTokens ${combo.maxTokens}`);
    if (!hasNum(combo.maxIter)) gaps.push(`maxIter ${combo.maxIter}`);
    if (!hasNum(combo.timeout)) gaps.push(`timeout ${combo.timeout}`);
    if (combo.builtin === "both" || combo.builtin === "browser") if (!/browser/i.test(text)) gaps.push("builtin browser");
    if (combo.builtin === "both" || combo.builtin === "code-interpreter") if (!/code.interpreter/i.test(text)) gaps.push("builtin code-interpreter");
  }
  if (combo.method === "zip_runtime") {
    if (combo.protocol === "a2a" && !/A2A/i.test(text)) gaps.push("protocol a2a");
    if (combo.protocol !== "a2a" && !/HTTP/i.test(text)) gaps.push("protocol http");
    if (combo.toolkit === "hr_assistant" && !/hr_assistant/i.test(text)) gaps.push("toolkit");
  }
  if (combo.attach === "gateway" || combo.attach === "gateway2") if (!env.gateways.some((g) => text.includes(g))) gaps.push("gateway attachment");
  if (combo.model === "custom-bogus" && !text.includes(BOGUS_MODEL)) gaps.push("custom model id");
  if (combo.method === "byoc") {
    if (combo.byocArtifact === "ecr-in-account" && !text.includes(ecrHost())) gaps.push("image uri");
  }
  return gaps;
}

/** The spec the wizard would send for a BLOCKED row: the method's captured base spec with the
 *  row's choices patched in (name = reserved, so a pass answers 409 and nothing deploys). */
function patchSpec(base: Record<string, unknown>, combo: Combo): Record<string, unknown> {
  const s = JSON.parse(JSON.stringify(base)) as Record<string, unknown>;
  s.name = L.RESERVED_NAME;
  const tools = Array.isArray(s.tools) ? (s.tools as Record<string, unknown>[]) : [];
  if (combo.method === "harness") {
    if (combo.maxTokens === "empty") delete s.max_tokens;
    else s.max_tokens = /^\d+$/.test(combo.maxTokens) ? Number(combo.maxTokens) : combo.maxTokens;
    s.max_iterations = Number(combo.maxIter);
    s.timeout_seconds = Number(combo.timeout);
    const builtins = combo.builtin === "both" ? ["code-interpreter", "browser"] : combo.builtin === "none" ? [] : [combo.builtin];
    s.tools = [...builtins.map((n) => ({ type: "builtin", name: n })), ...tools.filter((t) => t.type !== "builtin")];
    s.native_tools = combo.native === "default" ? ["shell", "file_operations"] : combo.native === "shell" ? ["shell"] : [];
  }
  if (combo.method !== "byoc") {
    s.filesystem = { session_storage: combo.fs === "off" ? null : { mount_path: combo.fs === "bad-mount" ? "/mnt" : "/mnt/workspace" } };
    s.memory = { short_term: true, long_term: combo.memory !== "lt-off" };
  }
  if (combo.method === "zip_runtime") {
    const a2a = combo.protocol === "a2a";
    s.protocol = a2a ? "a2a" : "http";
    if (a2a) {
      s.a2a_skills = combo.a2aSkills === "none" ? [] : [{ id: "calculator", name: "calculator", description: "Evaluate a basic arithmetic expression", tags: ["math"] }];
      s.tools = [];
      delete s.toolkits;
    } else {
      delete s.a2a_skills;
      if (combo.toolkit === "hr_assistant") s.toolkits = ["hr_assistant"];
      else delete s.toolkits;
    }
  }
  if (combo.identity === "rest-open" || combo.identity === "mcp-noconn") {
    const row = combo.identity === "rest-open" ? { type: "rest", name: "x16n_rest", config: { url: "https://example.invalid/x16n" }, auth: null } : { type: "mcp", name: "x16n_mcp", config: { url: "https://example.invalid/x16n" }, auth: null };
    s.tools = [...((s.tools as Record<string, unknown>[]) ?? []).filter((t) => t.type !== "rest" && t.type !== "mcp"), row];
  }
  if (combo.model === "custom-bogus") s.model_id = BOGUS_MODEL;
  if (combo.model === "unavailable" && env.unavailableModel) s.model_id = env.unavailableModel;
  if (combo.method === "byoc") {
    const b = (s.byoc as Record<string, unknown>) ?? {};
    const kind = combo.byocKind;
    const image = kind === "container_image";
    const next: Record<string, unknown> = { artifact_kind: kind, invoke_contract: b.invoke_contract ?? "launchpad_prompt", allowed_models: b.allowed_models ?? [s.model_id] };
    if (image) next.image_uri = combo.byocArtifact === "not-ecr" ? "docker.io/library/x16n:latest" : `${ecrHost()}/x16n/hello:latest`;
    else {
      next.upload_id = combo.byocArtifact === "none" ? "" : (b.upload_id ?? "");
      if (kind === "code_zip") Object.assign(next, { entrypoint: "main.py", python_version: "PYTHON_3_13", install_requirements: true });
    }
    s.byoc = next;
    if (combo.model === "custom-bogus") next.allowed_models = [BOGUS_MODEL];
  }
  return s;
}

type ServerVerdict = "accept" | 422 | 404 | 409 | number;
async function replay(spec: Record<string, unknown>): Promise<{ verdict: ServerVerdict; status: number; code: string; detail: string }> {
  const r = await admin.api.post("/api/agents", { ...spec, name: L.RESERVED_NAME });
  const code = String(r.body?.code ?? "");
  const detail = JSON.stringify(r.body?.detail ?? r.body?.message ?? "").slice(0, 300);
  if (r.status === 409 && code === "agent.name_reserved") return { verdict: "accept", status: r.status, code, detail };
  if (r.status === 202 || r.status === 201 || r.status === 200) {
    // must not happen with the reserved name — but if it does, remove the row at once
    const id = r.body?.agent?.id;
    if (id) await admin.api.delete(`/api/agents/${encodeURIComponent(id)}`);
    return { verdict: "accept", status: r.status, code: "CREATED(unexpected)", detail };
  }
  return { verdict: r.status as ServerVerdict, status: r.status, code, detail };
}

function cls(expUi: dom.Verdict, gotUi: dom.Verdict, expSrv: ServerVerdict, gotSrv: ServerVerdict | null): { verdict: L.Result["verdict"]; cls?: L.Cls; why: string } {
  const srvOk = gotSrv === null || gotSrv === expSrv || (typeof expSrv === "number" && typeof gotSrv === "number" && Math.floor(gotSrv / 100) === 4 && Math.floor(expSrv / 100) === 4);
  if (expUi === gotUi && srvOk) return { verdict: "PASS", why: "" };
  if (gotUi === "allowed" && gotSrv !== null && gotSrv !== "accept") return { verdict: "FAIL", cls: "INCONSISTENT", why: `UI allowed it, server ${gotSrv}` };
  if (gotUi === "blocked" && gotSrv === "accept") return { verdict: "FAIL", cls: "UX", why: "UI blocked something the server accepts" };
  if (expUi !== gotUi) return { verdict: "FAIL", cls: "INCONSISTENT", why: `oracle expected UI ${expUi}, wizard ${gotUi}` };
  return { verdict: "FAIL", cls: "INCONSISTENT", why: `server ${gotSrv} vs expected ${expSrv}` };
}

async function runCombo(page: Page, idx: string, combo: Combo, fixedName?: string) {
  const id = `${idx}-${combo.method}`;
  const exp = dom.expectAgent(combo, env);
  const name = fixedName ?? `x16n-c${idx.replace(/\D/g, "")}${TAG}-${combo.method.replace("_", "")}`.toLowerCase().slice(0, 48);
  const m = L.mark(admin);
  let d: Drive;
  try {
    d = await drive(page, combo, name, id);
  } catch (err) {
    L.record({ suite: SUITE, id, verdict: "BLOCKED", combo, expected: JSON.stringify(exp), observed: `drive error: ${String(err).slice(0, 300)}`, screenshots: [await L.shot(page, `${id}-error`)] });
    return;
  }
  const gotUi: dom.Verdict = d.uiBlocked ? "blocked" : "allowed";
  let gotSrv: ServerVerdict | null = null;
  let srvNote = "";
  let gaps: string[] = [];
  if (d.atReview && d.uiBlocked) {
    // the wizard explained itself by DISABLING the option and went on with a valid substitute:
    // the server side of the disabled value is checked in the blocked-row replay
    gaps = reviewGaps(combo, name, d.reviewText).filter((g) => !(combo.model === "unavailable" && g === "custom model id"));
    blockedPending.push({ id, combo, exp });
    srvNote = "disabled option → blocked-row replay";
  } else if (d.atReview) {
    gaps = reviewGaps(combo, name, d.reviewText);
    const wantLive = fixedName !== undefined || (exp.server === "accept" && state.liveCount < LIVE_CAP && !liveAgents.some((a) => a.name.endsWith(combo.method)));
    const live = wantLive && state.liveCount < LIVE_CAP && exp.server === "accept";
    let captured: Record<string, unknown> | null = null;
    if (!live) {
      await page.route("**/api/agents", async (route) => {
        if (route.request().method() !== "POST") return route.continue();
        captured = route.request().postDataJSON() as Record<string, unknown>;
        await route.fulfill({ status: 409, contentType: "application/json", body: JSON.stringify({ code: "x16n.intercepted", message: "intercepted by the combination suite (not sent)", detail: null }) });
      });
    }
    const submit = page.getByTestId("v2-agent-wizard-submit");
    if (await submit.isDisabled()) {
      srvNote = "submit disabled at review";
    } else {
      const resP = live ? page.waitForResponse((r) => r.url().endsWith("/api/agents") && r.request().method() === "POST", { timeout: 60_000 }).catch(() => null) : null;
      await submit.click();
      if (live) {
        const res = await resP;
        const status = res?.status() ?? 0;
        const body = res ? await res.json().catch(() => ({})) : {};
        captured = (res?.request().postDataJSON() as Record<string, unknown>) ?? null;
        if (status === 202 || status === 201) {
          state.liveCount += 1;
          liveAgents.push({ name, id: body.agent?.id });
          gotSrv = "accept";
          // the created row must match the request
          await page.waitForURL(/view=detail/, { timeout: 20_000 }).catch(() => (srvNote += " no detail navigation;"));
          const row = (await admin.api.get(`/api/agents/${encodeURIComponent(body.agent?.id ?? "")}`)).body ?? {};
          const spec = row.spec ?? {};
          const diffs: string[] = [];
          for (const k of ["method", "model_id", "protocol", "max_tokens", "max_iterations", "timeout_seconds", "toolkits", "native_tools"]) {
            if (captured && k in captured && JSON.stringify(captured[k]) !== JSON.stringify(spec[k])) diffs.push(`${k}: sent ${JSON.stringify(captured[k])} stored ${JSON.stringify(spec[k])}`);
          }
          if (captured && JSON.stringify((captured.memory as { long_term?: boolean })?.long_term) !== JSON.stringify(spec.memory?.long_term)) diffs.push("memory.long_term");
          const sentTools = ((captured?.tools as { type: string; name: string }[]) ?? []).map((t) => `${t.type}:${t.name}`).sort();
          const storedTools = ((spec.tools as { type: string; name: string }[]) ?? []).map((t) => `${t.type}:${t.name}`).sort();
          if (JSON.stringify(sentTools) !== JSON.stringify(storedTools)) diffs.push(`tools: sent ${sentTools} stored ${storedTools}`);
          if (row.name !== name || row.method !== combo.method) diffs.push(`row ${row.name}/${row.method}`);
          srvNote = diffs.length ? `created row differs: ${diffs.join("; ")}` : `created ${body.agent?.id} (deploying)`;
          if (diffs.length) gaps.push(...diffs.map((x) => `stored≠sent ${x}`));
        } else {
          gotSrv = status;
          srvNote = `live POST ${status} ${JSON.stringify(body).slice(0, 300)}`;
        }
      } else {
        await page.waitForTimeout(800);
        await page.unroute("**/api/agents");
        if (captured) {
          const r = await replay(captured);
          gotSrv = r.verdict;
          srvNote = `replay ${r.status} ${r.code} ${r.detail}`;
        } else srvNote = "no POST captured after submit";
      }
    }
    if (captured && !baseSpecs[combo.method as dom.AgentMethod] && exp.server === "accept" && gotSrv === "accept") baseSpecs[combo.method as dom.AgentMethod] = captured;
  } else {
    blockedPending.push({ id, combo, exp });
  }
  saveState();
  const verdict = cls(exp.ui, gotUi, exp.server, gotSrv);
  const sinceM = L.since(admin, m);
  const observed = [`ui=${gotUi}${d.uiReasons.length ? ` (${d.uiReasons.join(", ")})` : ""}`, gotSrv !== null ? `server=${gotSrv} ${srvNote}` : srvNote, gaps.length ? `review gaps: ${gaps.join(", ")}` : "", d.notes.join("; "), sinceM.pageErrors.length ? `pageErrors: ${sinceM.pageErrors.map((e) => e.text).join(" | ")}` : "", sinceM.consoleErrors.length ? `consoleErrors: ${sinceM.consoleErrors.length} (${sinceM.consoleErrors[0].text.slice(0, 120)})` : ""].filter(Boolean).join(" · ");
  const finalVerdict = verdict.verdict === "PASS" && (gaps.length || sinceM.pageErrors.length) ? "FAIL" : verdict.verdict;
  L.record({ suite: SUITE, id, verdict: finalVerdict, cls: finalVerdict === "FAIL" ? verdict.cls ?? (sinceM.pageErrors.length ? "BROKEN" : "INCONSISTENT") : undefined, combo, expected: `ui=${exp.ui} server=${exp.server} [${[...exp.uiReasons, ...exp.serverReasons].join(", ")}]`, observed: `${verdict.why ? `${verdict.why} · ` : ""}${observed}`, screenshots: d.screenshots, repro: { seed: SEED, name, combo: comboLabel(combo) } });
  expect.soft(finalVerdict, `${id} ${comboLabel(combo)} :: ${verdict.why || observed}`).toBe("PASS");
}

test("anchors: the live fixture rows (valid, one per method/protocol)", async () => {
  const page = admin.page;
  if (process.env.X16N_FRESH === "1") {
    // a fresh run: forget the persisted state once (here, not in beforeAll — Playwright re-runs
    // beforeAll in the new worker it starts after a failed test)
    // clear IN PLACE: `baseSpecs` / `blockedPending` / `liveAgents` alias these objects
    for (const k of Object.keys(state.baseSpecs)) delete state.baseSpecs[k as dom.AgentMethod];
    state.blockedPending.length = 0;
    state.liveAgents.length = 0;
    state.liveCount = 0;
    saveState();
  }
  for (const [i, a] of ANCHORS.entries()) {
    const have = (await L.listAgents(admin.api)).find((x) => x.name === a.name);
    if (have) {
      L.log("anchor exists", a.name, have.status);
      liveAgents.push({ name: a.name, id: have.id });
      state.liveCount += 1;
      continue;
    }
    await runCombo(page, `A${i}`, a.combo, a.name);
  }
});

test("pairwise rows: harness / zip_runtime / byoc", async () => {
  const page = admin.page;
  const r = allPairs(dom.agentFactors(env), { seed: SEED, constraint: dom.agentConstraint });
  for (const [i, combo] of r.combos.entries()) await runCombo(page, `P${String(i).padStart(2, "0")}`, combo);
});

test("blocked rows: the server refuses the same spec (reserved-name replay)", async () => {
  for (const b of blockedPending) {
    const base = baseSpecs[b.combo.method as dom.AgentMethod];
    if (!base) {
      L.record({ suite: SUITE, id: `${b.id}-server`, verdict: "BLOCKED", combo: b.combo, observed: `no captured base spec for ${b.combo.method}` });
      continue;
    }
    if (b.combo.model === "unavailable" && b.exp.uiReasons.length === 1) {
      // the provider check runs AFTER the reserved-name 409, so the reserved replay cannot reach
      // it; the real-name probe `model-unavailable` covers this rule (422 model.provider_unconfigured)
      L.record({ suite: SUITE, id: `${b.id}-server`, verdict: "PASS", combo: b.combo, observed: "disabled model option: server rule covered by probe-model-unavailable (real name, 422 model.provider_unconfigured)" });
      continue;
    }
    const spec = patchSpec(base, b.combo);
    const byoc = spec.byoc as { artifact_kind?: string; upload_id?: string } | undefined;
    if (b.combo.method === "byoc" && byoc && byoc.artifact_kind !== "container_image" && !byoc.upload_id && b.combo.byocArtifact !== "none") {
      L.record({ suite: SUITE, id: `${b.id}-server`, verdict: "BLOCKED", combo: b.combo, observed: "no staged upload id in the captured base spec to replay a zip-kind row" });
      continue;
    }
    const r = await replay(spec);
    const v = cls("blocked", "blocked", b.exp.server, r.verdict);
    L.record({ suite: SUITE, id: `${b.id}-server`, verdict: v.verdict, cls: v.cls, combo: b.combo, expected: `server=${b.exp.server} [${b.exp.serverReasons.join(", ")}]`, observed: `${v.why ? `${v.why} · ` : ""}replay ${r.status} ${r.code} ${r.detail}`, repro: { spec: L.redact(spec) } });
    expect.soft(v.verdict, `${b.id} server-side :: ${v.why} ${r.code}`).toBe("PASS");
  }
});

test("api probes: rows the wizard cannot build but the capability matrix must refuse", async () => {
  for (const p of dom.AGENT_API_PROBES) {
    const base = baseSpecs[p.method];
    if (!base) {
      L.record({ suite: SUITE, id: `probe-${p.id}`, verdict: "BLOCKED", observed: `no base spec for ${p.method}` });
      continue;
    }
    const spec: Record<string, unknown> = { ...JSON.parse(JSON.stringify(base)), ...p.patch };
    // probes that must reach the route's pin / model checks (after the reserved-name 409) use a real name
    const realName = ["memory-bogus", "model-unavailable", "tool-auth-unknown-conn"].includes(p.id);
    spec.name = realName ? `x16n-probe-${p.id}`.slice(0, 48) : L.RESERVED_NAME;
    const r = await admin.api.post("/api/agents", spec);
    const code = String(r.body?.code ?? "");
    let got: ServerVerdict;
    let created = "";
    if (r.status === 409 && code === "agent.name_reserved") got = "accept";
    else if (r.status < 300) {
      got = "accept";
      const id = r.body?.agent?.id;
      created = ` CREATED ${id} (deleted again)`;
      if (id) await admin.api.delete(`/api/agents/${encodeURIComponent(id)}`);
    } else got = r.status;
    const ok = got === p.expect || (typeof got === "number" && typeof p.expect === "number" && Math.floor(got / 100) === 4);
    const exact = got === p.expect;
    L.record({ suite: SUITE, id: `probe-${p.id}`, verdict: ok ? (exact ? "PASS" : "INFO") : "FAIL", cls: ok ? undefined : got === "accept" ? "INCONSISTENT" : "BROKEN", expected: `${p.expect} — ${p.rule}`, observed: `${r.status} ${code} ${JSON.stringify(r.body?.message ?? r.body?.detail ?? "").slice(0, 300)}${created}`, repro: { spec: L.redact(spec) } });
    expect.soft(ok, `probe ${p.id}: ${p.rule} → ${r.status} ${code}${created}`).toBeTruthy();
  }
});

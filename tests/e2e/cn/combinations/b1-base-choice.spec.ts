/**
 * BASE-CHOICE coverage (round 16, r16-n — user-directed layer that runs before pairwise).
 * For every form / wizard with option groups — AgentWizard, TaskWizard, the memory resource
 * editor (strategies + merge card), the custom evaluator form, the online-evaluation editor —
 * three kinds of submission:
 *   (a) ALL-ON: every non-exclusive control ticked, every free field filled with a valid
 *       NON-default value (dependent controls ticked in their enable order);
 *   (b) EACH-EXCLUSIVE: one submission per alternative of every mutually exclusive group,
 *       everything else kept at the all-on configuration;
 *   (c) ALL-OFF / MINIMAL: everything optional unticked, every optional field empty — empty
 *       must mean "default", never an invalid form (the N14-obs2 class).
 * Every submission asserts READ-BACK, not just 2xx: the request body carries each chosen value
 * (network capture) AND the GET / detail shows it. A silently dropped option is a FINDING
 * (INCONSISTENT) even when the submit succeeded; a dependent control that never becomes
 * reachable in the all-on order is a finding too.
 * Fixtures: x16n- only. Agents are deleted by w9 once their deploy settles (the deploy job
 * has no status check, so deleting a *deploying* row would leave the job running — see the
 * report); runs, online configs, evaluators and memory resources are deleted right after the
 * read-back.
 */
import { expect, test, type Locator, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";

import * as L from "./lib";

const SUITE = "base-choice";
const FX_AGENT = process.env.X16N_TASK_AGENT ?? "x16n-fx-harness";
const TAG = Date.now().toString(36).slice(-4);
const PROMPT = "You are x16n (base-choice). Answer in one short sentence.";
/** X16N_BC_ONLY=page[:id],… narrows a rerun (e.g. "evaluator-form,agent-wizard:byoc-container-image") */
const ONLY = (process.env.X16N_BC_ONLY ?? "").split(",").filter(Boolean);
const wanted = (page: string, id: string) => !ONLY.length || ONLY.includes(page) || ONLY.includes(`${page}:${id}`);

let admin: L.Session;
let zips: { http: string; container: string };
let ws = { account_id: "", region: "" };
let counts: Record<string, { allOn: number; exclusive: number; allOff: number; findings: number }> = {};
const created: { kind: string; id: string }[] = [];

function bump(page: string, kind: "allOn" | "exclusive" | "allOff", finding: boolean) {
  counts[page] ??= { allOn: 0, exclusive: 0, allOff: 0, findings: 0 };
  counts[page][kind] += 1;
  if (finding) counts[page].findings += 1;
}

test.beforeAll(async ({ browser }) => {
  admin = await L.session(browser, "admin");
  const w = (await admin.api.get("/api/workspaces")).body?.workspaces?.find((x: { id: string }) => x.id === "default");
  ws = { account_id: w?.account_id ?? "", region: w?.region ?? "" };
  const tmp = fs.mkdtempSync(path.join(process.env.TMPDIR ?? "/tmp", "x16n-bc-"));
  const samples = path.resolve(__dirname, "../../../../samples/byoc");
  zips = { http: path.join(tmp, "hello-http.zip"), container: path.join(tmp, "hello-container.zip") };
  execFileSync("zip", ["-q", "-r", zips.http, "."], { cwd: path.join(samples, "hello-http") });
  execFileSync("zip", ["-q", "-r", zips.container, "."], { cwd: path.join(samples, "hello-container") });
});
test.afterAll(async () => {
  L.writeJson("b1-counts.json", { counts, created });
  L.log("base-choice counts", counts);
  await admin.ctx.close();
});

/* ───────────── helpers ───────────── */
const field = (page: Page, label: RegExp): Locator => page.locator(".v2-field").filter({ has: page.locator("label", { hasText: label }) }).first();
async function setChecked(box: Locator, on: boolean) {
  if ((await box.isChecked()) !== on) await box.click();
}
const get = (o: unknown, pathStr: string): unknown => pathStr.split(".").reduce<unknown>((acc, k) => (acc && typeof acc === "object" ? (acc as Record<string, unknown>)[k] : undefined), o);
const same = (a: unknown, b: unknown) => JSON.stringify(sortish(a)) === JSON.stringify(sortish(b));
function sortish(v: unknown): unknown {
  if (Array.isArray(v)) return [...v].map(sortish).sort((x, y) => JSON.stringify(x).localeCompare(JSON.stringify(y)));
  return v;
}

interface Chosen {
  /** request-body path → expected value */
  body: Record<string, unknown>;
  /** detail (GET) path → expected value */
  detail: Record<string, unknown>;
  notes: string[];
}

interface Submission {
  page: string;
  id: string;
  kind: "allOn" | "exclusive" | "allOff";
  /** drives the form; returns the chosen values and clicks submit */
  drive: (page: Page) => Promise<Chosen>;
  /** the mutation to capture */
  post: RegExp;
  /** read the created row back; returns the GET body and its id */
  readBack: (res: Record<string, unknown>) => Promise<{ id: string | null; detail: Record<string, unknown> | null }>;
  /** remove the row (optional: agents wait for w9) */
  cleanup?: (id: string) => Promise<void>;
  /** the detail PAGE must show these strings (UI read-back) */
  detailUrl?: (id: string) => string;
  detailText?: string[];
}

async function run(s: Submission) {
  if (!wanted(s.page, s.id)) return;
  const page = admin.page;
  const m = L.mark(admin);
  const gaps: string[] = [];
  let status = 0;
  let resBody: Record<string, unknown> = {};
  let reqBody: Record<string, unknown> | null = null;
  let chosen: Chosen = { body: {}, detail: {}, notes: [] };
  let id: string | null = null;
  const shots: string[] = [];
  try {
    const resP = page.waitForResponse((r) => s.post.test(new URL(r.url()).pathname) && r.request().method() === "POST", { timeout: 90_000 });
    chosen = await s.drive(page);
    shots.push(await L.shot(page, `B-${s.page}-${s.id}-form`));
    const res = await resP;
    status = res.status();
    resBody = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    reqBody = (res.request().postDataJSON() as Record<string, unknown>) ?? null;
    if (status >= 300) gaps.push(`submit ${status} ${JSON.stringify(resBody).slice(0, 300)}`);
    else {
      for (const [p, v] of Object.entries(chosen.body)) {
        const got = get(reqBody, p);
        if (!same(got, v)) gaps.push(`request ${p}: sent ${JSON.stringify(got)} chose ${JSON.stringify(v)}`);
      }
      const rb = await s.readBack(resBody);
      id = rb.id;
      if (id) created.push({ kind: s.page, id });
      if (!rb.detail) gaps.push("no detail readable after create");
      else {
        for (const [p, v] of Object.entries(chosen.detail)) {
          const got = get(rb.detail, p);
          if (!same(got, v)) gaps.push(`DROPPED/CHANGED ${p}: stored ${JSON.stringify(got)} chose ${JSON.stringify(v)}`);
        }
      }
      if (id && s.detailUrl && s.detailText?.length) {
        await L.goto(page, s.detailUrl(id), 800);
        const text = await L.bodyText(page);
        for (const t of s.detailText) if (!text.includes(t)) gaps.push(`detail page lacks "${t}"`);
        shots.push(await L.shot(page, `B-${s.page}-${s.id}-detail`));
      }
    }
  } catch (err) {
    gaps.push(`drive error: ${String(err).split("\n")[0].slice(0, 200)}`);
    shots.push(await L.shot(page, `B-${s.page}-${s.id}-error`));
  }
  const d = L.since(admin, m);
  if (d.pageErrors.length) gaps.push(`pageErrors: ${d.pageErrors.map((e) => e.text).join(" | ").slice(0, 200)}`);
  const verdict: L.Result["verdict"] = gaps.length ? "FAIL" : "PASS";
  const cls: L.Cls | undefined = gaps.length ? (gaps.some((g) => g.startsWith("DROPPED") || g.startsWith("request")) ? "INCONSISTENT" : d.pageErrors.length ? "BROKEN" : "UX") : undefined;
  bump(s.page, s.kind, gaps.length > 0);
  L.record({ suite: SUITE, id: `${s.page}-${s.id}`, verdict, cls, expected: `${s.kind}: submit 2xx; request + GET carry ${Object.keys(chosen.body).length}/${Object.keys(chosen.detail).length} chosen values`, observed: [`status ${status}`, ...gaps, ...chosen.notes].join(" · "), screenshots: shots, repro: { request: reqBody ? L.redact(reqBody) : null, id } });
  expect.soft(verdict, `${s.page}/${s.id} :: ${gaps.join(" | ")}`).toBe("PASS");
  if (id && s.cleanup) await s.cleanup(id).catch((e) => L.log("cleanup failed", s.page, id, String(e).slice(0, 120)));
}

/* ───────────── AgentWizard ───────────── */
interface AgentCfg {
  method: "harness" | "zip_runtime" | "byoc";
  name: string;
  allOn: boolean;
  protocol?: "http" | "a2a";
  byocKind?: "code_zip" | "container_source" | "container_image";
}
const ecrHost = () => `${ws.account_id}.dkr.ecr.${ws.region}.${["amazonaws", "com", "cn"].join(".")}`;

async function driveAgent(page: Page, cfg: AgentCfg): Promise<Chosen> {
  const ch: Chosen = { body: {}, detail: {}, notes: [] };
  await L.goto(page, "/v2/agents?view=new", 300);
  await page.getByTestId(`v2-agent-method-${cfg.method}`).click();
  await page.getByTestId("v2-agent-wizard-next").click();
  await page.getByTestId("v2-agent-name").fill(cfg.name);
  ch.body.name = cfg.name;
  ch.detail.name = cfg.name;
  ch.detail.method = cfg.method;
  const on = cfg.allOn;
  if (cfg.method !== "byoc") {
    await page.getByTestId("v2-agent-prompt").fill(PROMPT);
    ch.body.system_prompt = PROMPT;
    ch.detail["spec.system_prompt"] = PROMPT;
    if (cfg.method === "zip_runtime" && cfg.protocol === "a2a") {
      await page.getByTestId("v2-agent-protocol-a2a").click();
      ch.body.protocol = "a2a";
      ch.detail["spec.protocol"] = "a2a";
      if (on) {
        await page.getByTestId("v2-agent-a2a-add").click();
        const rows = page.locator('[data-testid="v2-agent-a2a-skills"] .v2-agents-row');
        const last = (await rows.count()) - 1;
        await page.getByTestId(`v2-agent-a2a-name-${last}`).fill("x16n extra skill");
        await rows.nth(last).locator("input").nth(1).fill("an extra skill row");
        await rows.nth(last).locator("input").nth(2).fill("x16n, extra");
        ch.detail["spec.a2a_skills.2.name"] = "x16n extra skill";
        ch.detail["spec.a2a_skills.2.tags"] = ["x16n", "extra"];
      } else {
        // all-off: remove the seeded skill rows
        const rows = page.locator('[data-testid="v2-agent-a2a-skills"] .v2-agents-row');
        for (let n = await rows.count(); n > 0; n -= 1) await rows.nth(n - 1).locator("button").last().click();
        ch.body.a2a_skills = [];
      }
    }
    if (on) {
      // model: a non-default catalog pick
      const opts = await L.listOptions(page, page.getByTestId("v2-agent-model"));
      const alt = opts.find((o) => !o.disabled && /kimi/i.test(o.label));
      if (alt) {
        await L.pickOption(page, page.getByTestId("v2-agent-model"), /kimi/i);
        ch.body.model_id = "global.moonshotai.kimi-k3";
        ch.detail["spec.model_id"] = "global.moonshotai.kimi-k3";
      } else ch.notes.push("no alternative model available");
    }
    if (cfg.method === "harness") {
      const builtins = page.locator('[data-testid="v2-agent-builtins"] input[type=checkbox]');
      const native = page.locator('[data-testid="v2-agent-native-tools"] input[type=checkbox]');
      if (on) {
        for (let i = 0; i < (await builtins.count()); i += 1) if (!(await builtins.nth(i).isDisabled())) await setChecked(builtins.nth(i), true);
        for (let i = 0; i < (await native.count()); i += 1) await setChecked(native.nth(i), true);
        await page.getByTestId("v2-agent-max-tokens").fill("4096");
        await page.getByTestId("v2-agent-max-iterations").fill("50");
        await page.getByTestId("v2-agent-timeout").fill("900");
        ch.body.max_tokens = 4096;
        ch.body.max_iterations = 50;
        ch.body.timeout_seconds = 900;
        ch.detail["spec.max_tokens"] = 4096;
        ch.detail["spec.max_iterations"] = 50;
        ch.detail["spec.timeout_seconds"] = 900;
        ch.detail["spec.native_tools"] = ["shell", "file_operations"];
        // the reasoning effort is enabled for OpenAI models only — unreachable here by design
        const effort = field(page, /effort/i).locator(".v2-sel");
        if ((await effort.count()) && !(await effort.isDisabled())) ch.notes.push("effort select enabled on a non-OpenAI model");
      } else {
        for (let i = 0; i < (await builtins.count()); i += 1) await setChecked(builtins.nth(i), false);
        for (let i = 0; i < (await native.count()); i += 1) await setChecked(native.nth(i), false);
        await page.getByTestId("v2-agent-max-tokens").fill("");
        ch.detail["spec.native_tools"] = [];
        ch.detail["spec.max_tokens"] = null;
      }
    }
    if (on && (cfg.method === "harness" || cfg.protocol !== "a2a")) {
      const gws = page.locator('[data-testid="v2-agent-gateways"] input[type=checkbox]');
      const gn = await gws.count();
      for (let i = 0; i < gn; i += 1) await setChecked(gws.nth(i), true);
      if (!gn) ch.notes.push("no gateway offered");
      const skills = page.locator('[data-testid="v2-agent-skills"] input[type=checkbox]');
      if (await skills.count()) await setChecked(skills.first(), true);
      else ch.notes.push("no registry skill offered");
      const kbs = page.locator('[data-testid="v2-agent-kbs"] input[type=checkbox]');
      if (!(await kbs.count())) ch.notes.push("no KB offered (none on the host)");
      if (cfg.method === "zip_runtime") {
        await page.locator('[data-testid="v2-agent-toolkits"] input[type=checkbox]').first().check();
        ch.body.toolkits = ["hr_assistant"];
        ch.detail["spec.toolkits"] = ["hr_assistant"];
        await page.getByTestId("v2-agent-identity-add-rest").click();
        await page.getByTestId("v2-agent-identity-name-0").fill("x16n_rest");
        await page.getByTestId("v2-agent-identity-url-0").fill("https://example.invalid/x16n");
        const connOpts = await L.listOptions(page, page.getByTestId("v2-agent-identity-connection-0"));
        if (connOpts.filter((o) => o.label && !/none|choose/i.test(o.label)).length === 0) ch.notes.push("no Connection to pick (identity row stays open)");
      }
    }
    {
      const lt = page.getByTestId("v2-agent-long-term");
      await setChecked(lt, on);
      ch.body["memory.long_term"] = on;
      ch.detail["spec.memory.long_term"] = on;
      const fsBox = page.getByTestId("v2-agent-fs-session");
      await setChecked(fsBox, on);
      if (on) {
        await page.getByTestId("v2-agent-fs-session-mount").fill("/mnt/x16n");
        ch.body["filesystem.session_storage.mount_path"] = "/mnt/x16n";
        ch.detail["spec.filesystem.session_storage.mount_path"] = "/mnt/x16n";
      } else {
        ch.body["filesystem.session_storage"] = null;
        ch.detail["spec.filesystem.session_storage"] = null;
      }
      const policies = page.locator('[data-testid^="v2-wizard-policy-"]');
      if (on && !(await policies.count())) ch.notes.push("no policy to opt into (none on the host)");
    }
  } else {
    const kind = cfg.byocKind ?? "code_zip";
    await page.getByTestId(`v2-agent-byoc-kind-${kind}`).click();
    ch.body["byoc.artifact_kind"] = kind;
    ch.detail["spec.byoc.artifact_kind"] = kind;
    if (kind === "container_image") {
      await page.getByTestId("v2-agent-byoc-image").fill(`${ecrHost()}/x16n/hello:latest`);
      ch.detail["spec.byoc.image_uri"] = `${ecrHost()}/x16n/hello:latest`;
    } else {
      await page.getByTestId("v2-agent-byoc-file").setInputFiles(kind === "container_source" ? zips.container : zips.http);
      await page.getByTestId("v2-agent-byoc-uploaded").waitFor({ timeout: 150_000 });
    }
    if (on) {
      await page.getByTestId("v2-agent-byoc-desc").fill("x16n base-choice description");
      ch.body.system_prompt = "x16n base-choice description";
      ch.detail["spec.system_prompt"] = "x16n base-choice description";
      if (kind === "code_zip") {
        await L.pickOption(page, page.getByTestId("v2-agent-byoc-python"), /3\.12/);
        await setChecked(page.getByTestId("v2-agent-byoc-reqs"), false);
        ch.body["byoc.python_version"] = "PYTHON_3_12";
        ch.body["byoc.install_requirements"] = false;
        ch.detail["spec.byoc.python_version"] = "PYTHON_3_12";
        ch.detail["spec.byoc.install_requirements"] = false;
      }
      if (kind === "code_zip") {
        await setChecked(page.getByTestId("v2-agent-byoc-raw"), true);
        ch.body["byoc.invoke_contract"] = "raw";
        ch.detail["spec.byoc.invoke_contract"] = "raw";
      } else ch.notes.push("invoke contract toggle is code_zip-only (container kinds keep launchpad_prompt)");
      await L.pickOption(page, page.getByTestId("v2-agent-byoc-model-add"), /kimi/i);
      ch.detail["spec.byoc.allowed_models"] = ["global.zai.glm-5.3", "global.moonshotai.kimi-k3"];
      await page.getByTestId("v2-agent-byoc-env-add").click();
      await page.getByTestId("v2-agent-byoc-env-key-0").fill("X16N_FLAG");
      await page.getByTestId("v2-agent-byoc-env-value-0").fill("on");
      ch.body["env.X16N_FLAG"] = "on";
      ch.detail["spec.env.X16N_FLAG"] = "on";
    } else {
      ch.detail["spec.byoc.invoke_contract"] = "launchpad_prompt";
      if (kind === "code_zip") ch.detail["spec.byoc.install_requirements"] = true;
    }
  }
  await page.getByTestId("v2-agent-wizard-next").click();
  await page.getByTestId("v2-agent-review").waitFor({ timeout: 10_000 });
  await page.getByTestId("v2-agent-wizard-submit").click();
  return ch;
}

const agentSubmission = (id: string, kind: Submission["kind"], cfg: Omit<AgentCfg, "name">, text: string[] = []): Submission => ({
  page: "agent-wizard",
  id,
  kind,
  drive: (p) => driveAgent(p, { ...cfg, name: `x16n-b${TAG}-${id}`.slice(0, 48) }),
  post: /^\/api\/agents$/,
  readBack: async (res) => {
    const aid = String((res.agent as { id?: string })?.id ?? "");
    const row = aid ? (await admin.api.get(`/api/agents/${encodeURIComponent(aid)}`)).body : null;
    return { id: aid || null, detail: row };
  },
  detailUrl: (aid) => `/v2/agents?view=detail&id=${aid}`,
  detailText: [`x16n-b${TAG}-${id}`.slice(0, 48), ...text],
});

test("AgentWizard: all-on per method, each exclusive alternative, all-off", async () => {
  await run(agentSubmission("harness-allon", "allOn", { method: "harness", allOn: true }, ["global.moonshotai.kimi-k3"]));
  await run(agentSubmission("zip-http-allon", "allOn", { method: "zip_runtime", allOn: true, protocol: "http" }));
  await run(agentSubmission("byoc-zip-allon", "allOn", { method: "byoc", allOn: true, byocKind: "code_zip" }));
  // exclusive groups: protocol, byoc artifact kind (the method tabs are the three all-on rows)
  await run(agentSubmission("zip-a2a", "exclusive", { method: "zip_runtime", allOn: true, protocol: "a2a" }));
  await run(agentSubmission("byoc-container-source", "exclusive", { method: "byoc", allOn: true, byocKind: "container_source" }));
  await run(agentSubmission("byoc-container-image", "exclusive", { method: "byoc", allOn: true, byocKind: "container_image" }));
  // all-off / minimal
  await run(agentSubmission("harness-alloff", "allOff", { method: "harness", allOn: false }));
  await run(agentSubmission("zip-http-alloff", "allOff", { method: "zip_runtime", allOn: false, protocol: "http" }));
  await run(agentSubmission("zip-a2a-alloff", "allOff", { method: "zip_runtime", allOn: false, protocol: "a2a" }));
  await run(agentSubmission("byoc-alloff", "allOff", { method: "byoc", allOn: false, byocKind: "code_zip" }));
});

/* ───────────── TaskWizard ───────────── */
interface TaskCfg {
  target: "agent" | "cloudwatch";
  source: "window" | "sessions" | "logs" | "dataset";
  mode: "evaluators" | "insights";
  strategy: "history" | "continuous";
  allOn: boolean;
}
let datasetId = "";
const TASK_EVALS = ["Builtin.Helpfulness", "Builtin.Correctness", "Builtin.Faithfulness", "Builtin.Coherence", "Builtin.Conciseness"];

async function driveTask(page: Page, c: TaskCfg, name: string): Promise<Chosen> {
  const ch: Chosen = { body: {}, detail: {}, notes: [] };
  await L.goto(page, "/v2/eval/tasks?view=new", 300);
  await page.getByTestId("v2-task-name").waitFor({ timeout: 30_000 });
  if (c.target === "cloudwatch") {
    await page.getByTestId("v2-task-target-cloudwatch").click();
    await page.getByTestId("v2-task-cw-use").first().click();
  }
  await page.getByTestId("v2-task-name").fill(name);
  ch.body.name = name;
  if (c.target === "agent") await L.pickOption(page, page.getByTestId("v2-task-agent"), new RegExp(`^${FX_AGENT}$`));
  if (c.allOn) {
    await page.locator("textarea.v2-textarea").first().fill("x16n base-choice description");
    ch.body.description = "x16n base-choice description";
    ch.detail.description = "x16n base-choice description";
  }
  await page.getByTestId(`v2-task-mode-${c.mode}`).click();
  await page.getByTestId(`v2-task-src-${c.source}`).click();
  if (c.strategy === "continuous") {
    await page.getByTestId("v2-task-strategy-continuous").click();
    const nums = page.locator(".v2-form input[type=number]");
    await nums.nth(0).fill("25");
    await nums.nth(1).fill("30");
    ch.body.sampling_percentage = 25;
    ch.body.session_timeout_minutes = 30;
    ch.detail.sampling_percentage = 25;
    ch.detail.session_timeout_minutes = 30;
  } else if (c.source === "window" && c.allOn) {
    await L.pickOption(page, page.getByTestId("v2-task-lookback"), /^Last 72 hours$/);
    ch.body.lookback_hours = 72;
    ch.detail.dataset_name = "window:72h";
  } else if (c.source === "window") {
    ch.body.lookback_hours = 24; // the default must be sent, not dropped
  }
  if (c.source === "dataset") {
    await L.pickOption(page, page.getByTestId("v2-task-dataset"), /x16n-ds-gt/);
    ch.body.dataset_id = datasetId;
    ch.detail.dataset_id = datasetId;
  }
  if (c.source === "sessions") {
    await page.locator('input[data-testid^="v2-task-session-"]').first().waitFor({ timeout: 30_000 });
    await page.getByTestId("v2-task-sessions-all").click();
  }
  if (c.source === "logs") {
    await page.locator('input[data-testid^="v2-task-log-"]').first().waitFor({ timeout: 60_000 });
    await page.getByTestId("v2-task-logs-all").click();
  }
  await page.getByTestId("v2-task-next").click();
  if (c.mode === "evaluators") {
    const want = c.allOn ? TASK_EVALS : ["Builtin.Correctness", "Builtin.Helpfulness"];
    if (c.allOn) for (const e of TASK_EVALS) await page.getByTestId(`v2-task-eval-${e}`).check();
    ch.body.evaluators = want;
    ch.detail.evaluators = want;
  } else {
    const boxes = page.locator('input[data-testid^="v2-task-insight-"]');
    for (let i = 0; i < (await boxes.count()); i += 1) await setChecked(boxes.nth(i), true);
    ch.body.mode = "insights";
    ch.detail.mode = "insights";
  }
  await page.getByTestId("v2-task-submit").click();
  return ch;
}

const taskSubmission = (id: string, kind: Submission["kind"], cfg: TaskCfg): Submission => {
  const online = cfg.strategy === "continuous";
  return {
    page: "task-wizard",
    id,
    kind,
    drive: (p) => driveTask(p, cfg, `x16n-b${TAG}-${id}`.slice(0, 64)),
    post: online ? /^\/api\/eval\/online$/ : /^\/api\/eval\/runs$/,
    readBack: async (res) => {
      const rid = String(online ? res.config_id : res.id);
      const detail = (await admin.api.get(online ? `/api/eval/online/${encodeURIComponent(rid)}` : `/api/eval/runs/${encodeURIComponent(rid)}`)).body;
      return { id: rid, detail };
    },
    cleanup: async (rid) => {
      if (online) await admin.api.delete(`/api/eval/online/${encodeURIComponent(rid)}`);
      else {
        await admin.api.post(`/api/eval/runs/${encodeURIComponent(rid)}/stop`);
        await L.sleep(1500);
        await admin.api.delete(`/api/eval/runs/${encodeURIComponent(rid)}`);
      }
    },
    detailUrl: (rid) => (online ? `/v2/eval/tasks?view=detail&kind=online&id=${rid}` : `/v2/eval/tasks?view=detail&kind=run&id=${rid}`),
    detailText: [`x16n-b${TAG}-${id}`.slice(0, 64)],
  };
};

test("TaskWizard: all-on, each exclusive alternative (target / source / mode / strategy), all-off", async () => {
  const ds = await L.ensureDataset(admin.api, "x16n-ds-gt", L.GT_DATASET_ITEMS);
  datasetId = ds.id;
  const a = await L.waitForAgent(admin.api, FX_AGENT, "active", 20 * 60_000);
  if (!a) throw new Error(`${FX_AGENT} not active`);
  await run(taskSubmission("allon-dataset", "allOn", { target: "agent", source: "dataset", mode: "evaluators", strategy: "history", allOn: true }));
  await run(taskSubmission("window", "exclusive", { target: "agent", source: "window", mode: "evaluators", strategy: "history", allOn: true }));
  await run(taskSubmission("sessions", "exclusive", { target: "agent", source: "sessions", mode: "evaluators", strategy: "history", allOn: true }));
  await run(taskSubmission("logs", "exclusive", { target: "agent", source: "logs", mode: "evaluators", strategy: "history", allOn: true }));
  await run(taskSubmission("insights", "exclusive", { target: "agent", source: "dataset", mode: "insights", strategy: "history", allOn: true }));
  await run(taskSubmission("continuous", "exclusive", { target: "agent", source: "window", mode: "evaluators", strategy: "continuous", allOn: true }));
  await run(taskSubmission("cloudwatch", "exclusive", { target: "cloudwatch", source: "window", mode: "evaluators", strategy: "history", allOn: true }));
  await run(taskSubmission("alloff", "allOff", { target: "agent", source: "window", mode: "evaluators", strategy: "history", allOn: false }));
});

/* ───────────── memory resource editor ───────────── */
test("memory resource editor: all-on (3 strategies + 5 merge knobs), all-off", async () => {
  const drive = (allOn: boolean, name: string) => async (page: Page): Promise<Chosen> => {
    const ch: Chosen = { body: {}, detail: {}, notes: [] };
    await L.goto(page, "/v2/memory?tab=resources&view=resource-new", 300);
    await page.getByTestId("v2-memory-res-name").fill(name);
    ch.body.name = name;
    ch.detail.name = name;
    if (allOn) {
      await page.getByTestId("v2-memory-res-expiry").fill("30");
      await page.getByTestId("v2-memory-res-desc").fill("x16n base-choice memory");
      ch.body.event_expiry_days = 30;
      ch.body.description = "x16n base-choice memory";
      ch.detail.event_expiry_days = 30;
      ch.detail.description = "x16n base-choice memory";
      for (const k of ["semantic", "user_preference", "summarization"]) {
        const card = page.getByTestId(`v2-memory-strategy-${k}`);
        if (!/\bon\b/.test((await card.getAttribute("class")) ?? "")) await card.click();
      }
      ch.body.strategies = ["semantic", "user_preference", "summarization"];
      const merge: Record<string, number> = { recall_half_life_days: 45, max_facts_per_actor: 300, max_preferences_per_actor: 150, long_term_ttl_days: 60, actor_summary_interval: 7 };
      for (const [k, v] of Object.entries(merge)) {
        await page.getByTestId(`v2-memory-merge-${k}`).fill(String(v));
        ch.body[`merge.${k}`] = v;
        ch.detail[`merge.${k}`] = v;
      }
    } else {
      // every strategy card off, merge knobs empty (= defaults)
      for (const k of ["semantic", "user_preference", "summarization"]) {
        const card = page.getByTestId(`v2-memory-strategy-${k}`);
        if (/\bon\b/.test((await card.getAttribute("class")) ?? "")) await card.click();
      }
      ch.notes.push("all-off: no strategy, merge knobs empty");
    }
    const btn = page.getByTestId("v2-memory-res-create");
    if (await btn.isDisabled()) ch.notes.push(`create disabled: ${(await btn.getAttribute("title")) ?? ""}`);
    await btn.click();
    return ch;
  };
  const sub = (id: string, kind: Submission["kind"], allOn: boolean): Submission => ({
    page: "memory-resource",
    id,
    kind,
    drive: drive(allOn, `x16n_b${TAG}_${id}`.replace(/-/g, "_").slice(0, 48)),
    post: /^\/api\/memory\/resources$/,
    readBack: async (res) => {
      const mid = String(res.id ?? "");
      const detail = mid ? (await admin.api.get(`/api/memory/resources/${encodeURIComponent(mid)}`)).body : null;
      if (detail && Array.isArray(detail.strategies)) detail.strategies = (detail.strategies as { type?: string; kind?: string; strategy_type?: string }[]).map((s) => s.type ?? s.kind ?? s.strategy_type);
      return { id: mid || null, detail };
    },
    cleanup: async (mid) => {
      // DeleteMemory needs an ACTIVE (or FAILED) resource
      await L.waitFor(async () => {
        const d = (await admin.api.get(`/api/memory/resources/${encodeURIComponent(mid)}`)).body;
        return d?.status && d.status !== "CREATING" ? d : null;
      }, { timeoutMs: 8 * 60_000, everyMs: 10_000, label: `memory ${mid} settle` });
      const r = await admin.api.delete(`/api/memory/resources/${encodeURIComponent(mid)}`);
      L.log("delete memory resource", mid, r.status);
    },
    detailUrl: (mid) => `/v2/memory?tab=resources&view=resource&id=${mid}`,
    detailText: allOn ? ["x16n base-choice memory"] : [],
  });
  const allOn = sub("allon", "allOn", true);
  // the strategy list comes back as objects: compare the type set
  allOn.readBack = (async (res: Record<string, unknown>) => {
    const mid = String(res.id ?? "");
    const detail = mid ? (await admin.api.get(`/api/memory/resources/${encodeURIComponent(mid)}`)).body : null;
    if (detail?.strategies) detail.strategy_types = (detail.strategies as Record<string, unknown>[]).map((s) => String(s.type ?? s.kind ?? s.strategy_type ?? "")).map((t) => t.toLowerCase());
    return { id: mid || null, detail };
  }) as Submission["readBack"];
  await run(allOn);
  await run(sub("alloff", "allOff", false));
});

/* ───────────── custom evaluator form ───────────── */
test("evaluator form: judge all-on, derived (exclusive definition), judge all-off", async () => {
  const drive = (def: "judge" | "derived", allOn: boolean, name: string) => async (page: Page): Promise<Chosen> => {
    const ch: Chosen = { body: {}, detail: {}, notes: [] };
    await L.goto(page, "/v2/eval/evaluators?view=new", 300);
    await page.getByTestId(`v2-evaluator-def-${def}`).click();
    const code = page.getByTestId("v2-evaluator-def-code");
    if (!(await code.isDisabled())) ch.notes.push("code definition enabled although code evaluators are off");
    await page.getByTestId("v2-evaluator-name").fill(name);
    ch.body.name = name;
    ch.detail.name = name;
    if (def === "judge") {
      const rubric = allOn ? "Rate how well {output} answers {input}. Use the scale." : "Rate {output} against {input}.";
      await page.getByTestId("v2-evaluator-rubric").fill(rubric);
      ch.body.instructions = rubric;
      ch.detail.instructions = rubric;
    } else {
      await L.pickOption(page, field(page, /base/i).locator(".v2-sel"), /Helpfulness/);
      ch.body.base_evaluator_id = "Builtin.Helpfulness";
      ch.detail.base_evaluator_id = "Builtin.Helpfulness";
    }
    if (allOn) {
      await field(page, /description/i).locator("input").fill("x16n base-choice evaluator");
      ch.body.description = "x16n base-choice evaluator";
      ch.detail.description = "x16n base-choice evaluator";
      if (def === "judge") {
        await L.pickOption(page, field(page, /level/i).locator(".v2-sel"), /tool/i);
        ch.body.level = "TOOL_CALL";
        ch.detail.level = "TOOL_CALL";
      }
      const modelSel = field(page, /judge model|^\*?\s*model/i).locator(".v2-sel");
      const opts = await L.listOptions(page, modelSel);
      const alt = opts.find((o) => !o.disabled && /kimi/i.test(o.label));
      if (alt) {
        await L.pickOption(page, modelSel, /kimi/i);
        ch.body.model_id = "global.moonshotai.kimi-k3";
        ch.detail.model_id = "global.moonshotai.kimi-k3";
      } else ch.notes.push("no alternative judge model");
      if (def === "judge") {
        // add a scale point
        const add = page.locator("button").filter({ hasText: /add/i }).last();
        await add.click();
        const rows = page.locator(".v2-stack .v2-row");
        const last = rows.nth((await rows.count()) - 1);
        await last.locator("input").nth(0).fill("0.5");
        await last.locator("input").nth(1).fill("x16n_mid");
        await last.locator("input").nth(2).fill("half right");
        ch.notes.push("scale point added (0.5 / x16n_mid)");
      }
    }
    await page.getByTestId("v2-evaluator-save").click();
    return ch;
  };
  const sub = (id: string, kind: Submission["kind"], def: "judge" | "derived", allOn: boolean): Submission => ({
    page: "evaluator-form",
    id,
    kind,
    drive: drive(def, allOn, `x16n_b${TAG}_${id}`.replace(/-/g, "_").slice(0, 48)),
    post: /^\/api\/eval\/evaluators$/,
    readBack: async (res) => {
      const eid = String(res.evaluator_id ?? "");
      const detail = eid ? (await admin.api.get(`/api/eval/evaluators/${encodeURIComponent(eid)}`)).body : null;
      if (detail && Array.isArray(detail.rating_scale)) detail.scale_labels = (detail.rating_scale as { label: string }[]).map((p) => p.label);
      return { id: eid || null, detail };
    },
    cleanup: async (eid) => {
      const r = await admin.api.delete(`/api/eval/evaluators/${encodeURIComponent(eid)}`);
      L.log("delete evaluator", eid, r.status);
    },
    detailUrl: (eid) => `/v2/eval/evaluators?view=detail&id=${encodeURIComponent(eid)}`,
    detailText: allOn ? ["x16n base-choice evaluator"] : [],
  });
  const judgeOn = sub("judge-allon", "allOn", "judge", true);
  // the added scale point must survive: the stored labels end with ours
  judgeOn.readBack = async (res) => {
    const eid = String(res.evaluator_id ?? "");
    const detail = eid ? (await admin.api.get(`/api/eval/evaluators/${encodeURIComponent(eid)}`)).body : null;
    if (detail && Array.isArray(detail.rating_scale)) detail.scale_has_x16n = (detail.rating_scale as { label: string }[]).some((p) => p.label === "x16n_mid");
    return { id: eid || null, detail };
  };
  const judgeDrive = judgeOn.drive;
  judgeOn.drive = async (p) => {
    const ch = await judgeDrive(p);
    ch.detail.scale_has_x16n = true;
    return ch;
  };
  await run(judgeOn);
  await run(sub("derived", "exclusive", "derived", true));
  await run(sub("judge-alloff", "allOff", "judge", false));
});

/* ───────────── online evaluation editor ───────────── */
test("online editor: scores all-on, insights (exclusive mode), all-off", async () => {
  const a = await L.waitForAgent(admin.api, FX_AGENT, "active", 20 * 60_000);
  if (!a) throw new Error(`${FX_AGENT} not active`);
  const drive = (mode: "scores" | "insights", allOn: boolean, desc: string) => async (page: Page): Promise<Chosen> => {
    const ch: Chosen = { body: {}, detail: {}, notes: [] };
    await L.goto(page, "/v2/eval/online?view=new", 300);
    await L.pickOption(page, page.getByTestId("v2-online-agent"), new RegExp(`^${FX_AGENT}\\b`));
    ch.body.agent_id = a.id;
    ch.detail.agent_id = a.id;
    await page.getByTestId("v2-online-desc").fill(desc);
    ch.body.description = desc;
    ch.detail.description = desc;
    if (mode === "insights") await page.getByTestId("v2-online-mode-insights").click();
    const enable = field(page, /enable/i).locator("input[type=checkbox]");
    if (await enable.count()) {
      await setChecked(enable, allOn);
      ch.body.enable_on_create = allOn;
    }
    if (mode === "scores") {
      const evals = allOn ? TASK_EVALS.slice(0, 3) : [];
      // the editor preselects two evaluators: all-on replaces them with our explicit pick
      if (allOn) for (const b of await page.locator('input[data-testid^="v2-online-eval-"]:checked').all()) await b.uncheck();
      for (const e of evals) await page.getByTestId(`v2-online-eval-${e}`).check();
      if (allOn) {
        ch.body.evaluators = evals;
        ch.detail.evaluators = evals;
      } else {
        const picked = await page.locator('input[data-testid^="v2-online-eval-"]:checked').count();
        ch.notes.push(`all-off: ${picked} evaluators preselected`);
      }
    } else {
      const boxes = field(page, /insight/i).locator("input[type=checkbox]");
      for (let i = 0; i < (await boxes.count()); i += 1) await setChecked(boxes.nth(i), true);
      const freqs = field(page, /frequenc/i).locator("input[type=checkbox]");
      for (let i = 0; i < (await freqs.count()); i += 1) await setChecked(freqs.nth(i), i < 2);
      ch.body.clustering_frequencies = ["DAILY", "WEEKLY"];
      ch.detail.clustering_frequencies = ["DAILY", "WEEKLY"];
    }
    if (allOn) {
      await page.getByTestId("v2-online-sampling").fill("25");
      await field(page, /timeout/i).locator("input").fill("30");
      ch.body.sampling_percentage = 25;
      ch.body.session_timeout_minutes = 30;
      ch.detail.sampling_percentage = 25;
      ch.detail.session_timeout_minutes = 30;
      // one filter row
      const addFilter = field(page, /filter/i).locator("button").filter({ hasText: /add/i }).first();
      if (await addFilter.count()) {
        await addFilter.click();
        const keyInput = field(page, /filter/i).locator("input").first();
        await keyInput.fill("x16n.key");
        const valueInput = field(page, /filter/i).locator("input").last();
        await valueInput.fill("x16n");
        ch.notes.push("one filter row added (x16n.key = x16n)");
      } else ch.notes.push("no add-filter button");
    } else ch.body.session_timeout_minutes = 15;
    await page.getByTestId("v2-online-submit").click();
    return ch;
  };
  const sub = (id: string, kind: Submission["kind"], mode: "scores" | "insights", allOn: boolean): Submission => ({
    page: "online-editor",
    id,
    kind,
    drive: drive(mode, allOn, `x16n-b${TAG}-${id}`),
    post: /^\/api\/eval\/online$/,
    readBack: async (res) => {
      const cid = String(res.config_id ?? "");
      const detail = cid ? (await admin.api.get(`/api/eval/online/${encodeURIComponent(cid)}`)).body : null;
      return { id: cid || null, detail };
    },
    cleanup: async (cid) => {
      const r = await admin.api.delete(`/api/eval/online/${encodeURIComponent(cid)}`);
      L.log("delete online config", cid, r.status);
    },
    detailUrl: (cid) => `/v2/eval/online?view=detail&id=${cid}`,
    detailText: [`x16n-b${TAG}-${id}`],
  });
  await run(sub("scores-allon", "allOn", "scores", true));
  await run(sub("insights", "exclusive", "insights", true));
  await run(sub("alloff", "allOff", "scores", false));
});

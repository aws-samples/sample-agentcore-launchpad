import { expect, test, type APIRequestContext, type Locator, type Page, type TestInfo } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { attach, awsRead, E2E_PREFIX, evidence, ledger, listAgents, sleep } from "../helpers";

/**
 * Shared module of the end-to-end SCENARIO suite (`docs/cn/reports/e2e-scenario-plan.md`).
 *
 * One business storyline — the "Octank HR assistant" — runs through every module in order
 * (s00 → s09, then s11 govern, then s12 model providers, then s10 lifecycle — s11 needs the HR
 * assistant s10 deletes; s12 is admin-only and hermetic to the console + API),
 * each step consuming the artefacts of the previous one through a small JSON state file. Run it with its own Playwright config so the defaults below apply:
 *
 *   cd tests/e2e && npx playwright test -c scenario.config.ts            # whole storyline
 *   SCENARIO_RESUME_FROM=s07 npx playwright test -c scenario.config.ts    # resume at step 7
 *   npx playwright test -c scenario.config.ts cn/scenario/scenario-cleanup.spec.ts
 *
 * Everything the storyline creates is named `e2es-…` (evaluators `e2es_…`, no dashes allowed)
 * and is removed by `scenario-cleanup.spec.ts` in reverse dependency order.
 */

// ── naming ────────────────────────────────────────────────────────────────
/** `scenario.config.ts` pins N10_PREFIX=e2es- so the shared helpers (`e2eName`, `isE2E`) agree. */
export const SCN_PREFIX = E2E_PREFIX;
export const SCN_EVALUATOR_PREFIX = SCN_PREFIX.replace(/-$/, "_");
export const isScn = (name: unknown) => String(name ?? "").startsWith(SCN_PREFIX);
export const isScnEvaluator = (name: unknown) => String(name ?? "").startsWith(SCN_EVALUATOR_PREFIX);
/** Cedar policy names (`POLICY_NAME_PATTERN`, no dashes) share the evaluator spelling: `e2es_<thing>_<stamp>`. */
export const SCN_POLICY_PREFIX = SCN_EVALUATOR_PREFIX;
export const isScnPolicy = (name: unknown) => String(name ?? "").startsWith(SCN_POLICY_PREFIX);

export const JUDGE_DEFAULT = "global.zai.glm-5.3";
export const JUDGE_ALT = "global.moonshotai.kimi-k3";
export const GATEWAY_TARGET = "hr-database";
export const GATEWAY_TOOL = "hr-database___get_employee";
export const EVAL_WORKER_PRESET = "eval-worker";
/** The HR skill of the storyline (platform-seeded `samples/skills/expense-report-writer`). */
export const HR_SKILL = "expense-report-writer";
/** Fallback pair when the generated task set cannot drive the trainer (plan §3, open question Q1). */
export const FALLBACK_SKILL = "sample-logtriage-v2";
export const FALLBACK_TASKSET = "Log triage demo tasks";

export const FIXTURES = path.resolve(__dirname, "fixtures");
export const HR_DOCS = ["octank-hr-handbook.md", "octank-expense-policy.md"].map((f) => path.join(FIXTURES, f));

// ── storyline constants (prompts and the tokens the assertions look for) ───
export const TOKENS = {
  /** payroll A2A specialist: every answer carries it (s04a, s05 A2A turn) */
  payroll: "PAYROLL-A2A-7731",
  /** HR assistant "hotline code": the Chat vs /v1 parity probe (s05, s09) */
  hotline: "OCTANK-HR-0426",
  /** HR runtime twin "magic word" (s05 sessions on B, s09 after promote) */
  runtimeMagic: "HR-RUNTIME-42",
  /** canary candidate prompt marker (s08c) */
  canary: "Cheers!",
} as const;

export const PROMPTS = {
  payroll:
    `You are the Octank payroll specialist. Whatever you are asked, answer in one or two sentences and always include the exact token ${TOKENS.payroll}. ` +
    "Net pay at Octank is gross pay minus 10 % pension and minus income tax at 20 % of the remainder.",
  hrAssistant:
    "You are the Octank HR assistant for employees in China. Answer briefly in English. " +
    `When asked for the HR hotline code, reply exactly: ${TOKENS.hotline}. ` +
    "For ANY policy or handbook question you MUST call kb_search first and answer only from the retrieved passages, quoting the figures. " +
    "For ANY question about a specific employee id you MUST call the hr-database tool get_employee and quote the returned fields. " +
    "For ANY payroll or net-pay question you MUST ask the payroll specialist agent tool and relay its answer verbatim, including any token it returns. " +
    "For ANY arithmetic you MUST run Python in the code interpreter tool and answer with the exact integer. " +
    `For expense reports follow the ${HR_SKILL} skill. Remember personal facts the user shares about themselves.`,
  hrRuntime:
    "You are the Octank HR self-service runtime (Strands). Answer briefly and politely in English. " +
    `When asked for the magic word, reply exactly: ${TOKENS.runtimeMagic}. ` +
    "For policy questions call kb_search first and quote the figures; for a specific employee id call the hr-database get_employee tool.",
} as const;

/** Facts told in s05 session 1 and recalled in a later session (REQUIREMENTS §4.1 case 6). */
export const MEMORY_FACTS = { manager: "Li Wei", costCenter: "CC-7788" } as const;
/** KB question answered from `octank-expense-policy.md` (45 days, line manager). */
export const KB_QUESTION = "Within how many days must an expense claim be filed, and who approves it?";
export const KB_EXPECT = /45\s*(calendar\s*)?days/i;
export const KB_TOP1_DOC = /octank-expense-policy/;
/** Gateway fixture (`samples/hr_database_lambda/handler.py`): EMP-1024 = Maya Chen · HR Operations. */
export const TOOL_QUESTION = "Look up employee EMP-1024: what are their name, department and remaining vacation days this year?";
/** Code interpreter arithmetic: 13 × 8750 + 12 × 1250 = 128750. */
export const CI_QUESTION = "Use the code interpreter to compute the exact total of 13 months times 8750 plus 12 times 1250. Reply with the exact integer.";
export const CI_EXPECT = "128750";
/** Skill task: 420 + 38 + 610 = 1068. */
export const SKILL_QUESTION =
  "Draft my expense report: employee EMP-1024, 2026-10-01 to 2026-10-03, travel $420 (receipt yes), meals $38 (receipt yes), lodging $610 (receipt yes).";

// ── step order / resume ───────────────────────────────────────────────────
/** Run order, not numeric order: s11 (govern, D20) and s12 (model providers, D21) run before s10 (lifecycle deletes the HR assistant s11 governs; s12 reads the member identity s10 leaves alone). */
export const STEPS = ["s00", "s01", "s02", "s03", "s04", "s05", "s06", "s07", "s08", "s09", "s11", "s12", "s10"] as const;
export type Step = (typeof STEPS)[number];

/**
 * `SCENARIO_RESUME_FROM=sNN` skips every step before sNN (their artefacts are read back from
 * the state file). Call at the top of each step's test body.
 */
export function stepGate(step: Step): void {
  const from = process.env.SCENARIO_RESUME_FROM;
  if (!from) return;
  const at = STEPS.indexOf(from as Step);
  if (at < 0) throw new Error(`SCENARIO_RESUME_FROM=${from} is not one of ${STEPS.join(",")}`);
  test.skip(STEPS.indexOf(step) < at, `resumed from ${from} — ${step} is read back from the state file`);
}

// ── shared state (git-ignored, next to the auth state) ─────────────────────
export interface ScenarioState {
  run_start?: string;
  stamp?: string;
  member?: { username: string; password: string; id?: string };
  kb?: { id: string; name: string; indexed: number; ingest_seconds: number };
  gateway?: { record_id: string; tools: string[] };
  skill?: {
    name: string;
    record_id: string;
    version_before: string;
    version_after?: string;
    fallback: boolean;
    taskset_id?: string;
    jobs: { taskgen?: string; train?: string; eval?: string };
  };
  payroll?: { id: string; name: string; record_id?: string; deploy_seconds: number };
  hr?: { id: string; name: string; version: number; deploy_seconds: number };
  runtime?: { id: string; name: string; resource_id: string; version: number; deploy_seconds: number };
  sessions?: { hr: string[]; runtime: string[]; facts?: string; recall?: string };
  observe?: { first_trace_lag_s: number };
  evaluators?: { judge: string; kimi: string };
  dataset?: { id: string; name: string; items: number; cloud_id?: string };
  runs?: { hr_batch?: string; runtime_batch?: string; insights?: string; window?: string };
  online?: { config_id: string };
  improve?: {
    recommendation_id?: string;
    hr_version_after_accept?: number;
    runtime_version_after_promote?: number;
    canary_id?: string;
    canary_outcome?: "complete" | "rollback";
  };
  go_live?: { hr_version: number; runtime_version: number };
  /** s11 govern (D20 Policy): the `e2es_` policy ids, the enforce confirmation target and the audit window. */
  govern?: {
    workspace_id: string;
    workspace_name: string;
    audit_since: string;
    tool_pattern?: string;
    /** false when the HR spec declares a literal tool, not the `hr-database___*` pattern — s11e's literal forbid then REMOVES the tool for everyone */
    tool_is_pattern?: boolean;
    policies?: { permit_tools: string; forbid_tool: string; permit_invoke: string; forbid_invoke?: string };
    log_only_session?: string;
    enforce_at?: string;
    apply_job?: string;
    coverage?: string;
    hr_version_after_apply?: number;
    restored?: boolean;
  };
  /** s12 model providers (D21): the s12a baseline, the `e2es-mp-<stamp>` provider and its rows. */
  model_providers?: {
    baseline_version: number;
    baseline_models: string[];
    baseline_provider_names: string[];
    baseline_row_count: number;
    journal_before: number;
    sf_configured: boolean;
    provider_id?: string;
    provider_name?: string;
    chat_row_id?: string;
    chat_model_id?: string;
    embed_row_id?: string;
    embed_model_id?: string;
    embedding_branch?: "refused" | "flipped";
    workspace_id?: string;
    cleaned?: boolean;
  };
  lifecycle?: { hr_version_after_edit: number; deleted: boolean };
}

const STATE_FILE = path.resolve(__dirname, "../../.auth/scenario-state.json");

export function stateAll(): ScenarioState {
  try {
    return JSON.parse(fs.readFileSync(STATE_FILE, "utf8")) as ScenarioState;
  } catch {
    return {};
  }
}

export function stateGet<K extends keyof ScenarioState>(key: K): ScenarioState[K] {
  return stateAll()[key];
}

export function stateSet<K extends keyof ScenarioState>(key: K, value: ScenarioState[K]): void {
  const cur = stateAll();
  cur[key] = value;
  fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
  // lead ruling Q8: the throwaway member credential lives here → owner-only, never logged
  fs.writeFileSync(STATE_FILE, JSON.stringify(cur, null, 2), { mode: 0o600 });
  fs.chmodSync(STATE_FILE, 0o600);
}

/** Read a prerequisite produced by an earlier step, or fail with the step to (re)run. */
export function stateRequire<K extends keyof ScenarioState>(key: K, producedBy: Step): NonNullable<ScenarioState[K]> {
  const v = stateGet(key);
  if (v == null) throw new Error(`scenario state '${key}' missing — run ${producedBy} first (or set SCENARIO_RESUME_FROM accordingly)`);
  return v as NonNullable<ScenarioState[K]>;
}

/** One stamp per storyline run so every `e2es-<thing>-<stamp>` of a run shares it. */
export function runStamp(): string {
  let s = stateGet("stamp");
  if (!s) {
    s = new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "");
    stateSet("stamp", s);
    stateSet("run_start", new Date().toISOString());
  }
  return s;
}

/** `e2es-<suffix>-<stamp>`, valid for AgentSpec.name (^[a-z][a-z0-9-]{2,47}$). */
export function scnName(suffix: string): string {
  return `${SCN_PREFIX}${suffix}-${runStamp()}`.slice(0, 48).replace(/-+$/, "");
}

// ── evidence / ledger ─────────────────────────────────────────────────────
/** Curated evidence (lead ruling Q6): at most ~20 key screenshots per run are committed here; the bulk stays in the git-ignored `scenario/all/`. */
export const KEY_EVIDENCE_DIR = path.resolve(__dirname, "../../../../docs/cn/evidence/scenario");

/**
 * Screenshot (`sNN-<name>.png` under the bulk evidence dir) + one ledger line.
 * `{ key: true }` additionally copies the shot into the curated, committed folder.
 */
export async function snap(page: Page, name: string, note?: string, opts: { key?: boolean } = {}): Promise<void> {
  const file = await evidence(page, name);
  if (opts.key) {
    fs.mkdirSync(KEY_EVIDENCE_DIR, { recursive: true });
    fs.copyFileSync(file, path.join(KEY_EVIDENCE_DIR, path.basename(file)));
  }
  ledger(`evidence ${path.basename(file)}${opts.key ? " [key]" : ""}${note ? ` — ${note}` : ""}`);
}

/**
 * N-SCN-obs19 (test-side): navigate (or reload) and wait for the V2 page to RENDER before a `snap` — the auth gate gone,
 * the network idle and, when given, a marker (entity name / id text or a locator) visible. Every wait is bounded and
 * swallowed with a ledger note: evidence quality only, never grading. Eight `[key]` shots of the 2026-10-16 run were
 * 5 KB blanks because `snap` fired right after `page.goto`.
 */
export async function gotoRendered(page: Page, url: string | null, marker?: string | RegExp | Locator, timeoutMs = 60_000): Promise<void> {
  if (url === null) await page.reload();
  else await page.goto(url);
  await page.getByTestId("v2-auth-loading").waitFor({ state: "detached", timeout: 30_000 }).catch(() => {});
  await page.waitForLoadState("networkidle", { timeout: 30_000 }).catch(() => {});
  if (marker === undefined) return;
  const loc = typeof marker === "string" || marker instanceof RegExp ? page.getByText(marker).first() : marker;
  await loc.waitFor({ state: "visible", timeout: timeoutMs }).catch(() => {
    ledger(`note: N-SCN-obs19 render marker not seen within ${Math.round(timeoutMs / 1000)}s at ${url ?? "(reload)"} — shot taken anyway`);
  });
}

export async function record(testInfo: TestInfo, name: string, body: unknown, summary?: string): Promise<void> {
  await attach(testInfo, name, body);
  if (summary) ledger(`${name}: ${summary}`);
}

/** Mask anything that looks like an API key / bearer token before it lands in evidence text. */
export function mask(s: string): string {
  return s.replace(/(lp_[A-Za-z0-9]{6})[A-Za-z0-9_-]{8,}/g, "$1…").replace(/(Bearer\s+)[^\s"]+/gi, "$1…");
}

/**
 * Soft check that CANNOT fail the test: Playwright's `expect.soft` only avoids aborting the test body but still marks
 * the test failed at the end, which is not what the §6 "soft" category means (model / ranking / timing behaviour is
 * recorded, never graded). `softly` runs the assertion, and on a miss writes a `soft:` ledger line + a `soft-miss`
 * annotation and returns false.
 */
export function softly(testInfo: TestInfo, label: string, check: () => void): boolean {
  try {
    check();
    return true;
  } catch (e) {
    const msg = String((e as Error).message ?? e).split("\n").slice(0, 3).join(" ").replace(/\u001b\[[0-9;]*m/g, "");
    ledger(`soft: ${label} — ${msg.slice(0, 220)}`);
    testInfo.annotations.push({ type: "soft-miss", description: `${label}: ${msg.slice(0, 300)}` });
    return false;
  }
}

// ── generic pollers ───────────────────────────────────────────────────────
export async function pollUntil<T>(
  api: APIRequestContext,
  url: string,
  done: (body: T) => boolean,
  opts: { timeoutMs?: number; intervalMs?: number; label?: string; key?: (b: T) => string } = {},
): Promise<{ body: T; seconds: number }> {
  const timeoutMs = opts.timeoutMs ?? 10 * 60 * 1000;
  const intervalMs = opts.intervalMs ?? 10_000;
  const started = Date.now();
  let lastKey = "";
  let last: T | undefined;
  while (Date.now() - started < timeoutMs) {
    // a GET that gets no response within the request timeout (N-SCN-obs11) is a missed poll, not a failure
    const res = await api.get(url, { timeout: 60_000 }).catch(() => null);
    if (res?.ok()) {
      last = (await res.json()) as T;
      if (opts.key) {
        const k = opts.key(last);
        if (k !== lastKey) {
          lastKey = k;
          ledger(`${opts.label ?? url} +${Math.round((Date.now() - started) / 1000)}s ${k.slice(0, 200)}`);
        }
      }
      if (done(last)) return { body: last, seconds: Math.round((Date.now() - started) / 1000) };
    }
    await sleep(intervalMs);
  }
  throw new Error(`${opts.label ?? url} not settled after ${Math.round(timeoutMs / 1000)}s (last=${JSON.stringify(last).slice(0, 300)})`);
}

export interface EvalRun {
  id: string;
  status: string;
  progress?: string | null;
  scores?: unknown;
  error?: string | null;
  insights?: unknown;
  [k: string]: unknown;
}

const LIVE_RUN = new Set(["queued", "pending", "running", "invoking", "waiting", "evaluating", "submitting", "replaying"]);

/** Poll an evaluation run to a terminal state, ledgering every status/progress transition. */
export async function waitForRun(api: APIRequestContext, runId: string, label: string, timeoutMs = 30 * 60 * 1000): Promise<{ run: EvalRun; seconds: number }> {
  const started = Date.now();
  let lastKey = "";
  let last: EvalRun | undefined;
  while (Date.now() - started < timeoutMs) {
    const res = await api.get(`/api/eval/runs/${runId}`, { timeout: 60_000 }).catch(() => null);
    if (res?.ok()) {
      last = (await res.json()) as EvalRun;
      const key = `${last.status}|${String(last.progress ?? "")}`.slice(0, 160);
      if (key !== lastKey) {
        lastKey = key;
        ledger(`${label} run ${runId} +${Math.round((Date.now() - started) / 1000)}s ${key}`);
      }
      if (!LIVE_RUN.has(String(last.status).toLowerCase())) return { run: last, seconds: Math.round((Date.now() - started) / 1000) };
    }
    await sleep(10_000);
  }
  throw new Error(`${label}: run ${runId} still ${last?.status ?? "unknown"} after ${Math.round(timeoutMs / 1000)}s`);
}

/** POST a JSON action and ledger the outcome (canary stage driver). */
export async function act(api: APIRequestContext, url: string, body: Record<string, unknown>, label: string): Promise<{ status: number; body: Record<string, unknown> }> {
  const res = await api.post(url, { data: body, timeout: 120_000 });
  const text = await res.text();
  let parsed: Record<string, unknown> = {};
  try {
    parsed = JSON.parse(text) as Record<string, unknown>;
  } catch {
    parsed = { raw: text };
  }
  ledger(`${label} POST ${url} ${JSON.stringify(body).slice(0, 120)} → ${res.status()} ${text.slice(0, 200).replace(/\n/g, " ")}`);
  return { status: res.status(), body: parsed };
}

/** Wait until a canary row has no `running_action` and carries `artifacts[key]` (or failed). */
export async function settle(api: APIRequestContext, url: string, key: string, label: string, timeoutMs = 25 * 60 * 1000) {
  return pollUntil<Record<string, unknown>>(
    api,
    url,
    (b) => !b.running_action && ((b.artifacts as Record<string, unknown> | undefined)?.[key] != null || /failed|error/i.test(String(b.status))),
    { timeoutMs, intervalMs: 10_000, label, key: (b) => `${b.status}/${b.stage} running=${b.running_action ?? "-"} progress=${String(b.progress ?? "").slice(0, 100)}` },
  );
}

// ── V2 widgets ────────────────────────────────────────────────────────────
/** V2 dropdown: a trigger button (`data-testid`) opening a `role=listbox` of `role=option` rows. */
export async function pickOption(page: Page, triggerTestId: string, label: RegExp | string): Promise<void> {
  await page.getByTestId(triggerTestId).click();
  const list = page.getByRole("listbox");
  await expect(list).toBeVisible();
  await list.getByRole("option", { name: label }).first().click();
}

// ── read-only AWS probes (describe/get/list only, CN profile) ─────────────
export function runtimeEndpoints(runtimeId: string): Array<{ name: string; status: string; liveVersion?: string }> {
  const raw = awsRead(`bedrock-agentcore-control list-agent-runtime-endpoints --agent-runtime-id ${runtimeId}`);
  try {
    return (JSON.parse(raw) as { runtimeEndpoints?: Array<{ name: string; status: string; liveVersion?: string }> }).runtimeEndpoints ?? [];
  } catch {
    return [];
  }
}

export function runtimeIdOf(agentRow: Record<string, unknown>): string {
  return String(agentRow.resource_id ?? String(agentRow.arn ?? "").split("/").pop() ?? "");
}

// ── product lookups ───────────────────────────────────────────────────────
export async function agentByName(api: APIRequestContext, name: string): Promise<Record<string, unknown> | undefined> {
  return (await listAgents(api)).find((a) => String(a.name) === name);
}

export async function chatSessions(api: APIRequestContext, agentId: string): Promise<Array<{ session_id: string; turns: number; last_at: string }>> {
  const res = await api.get(`/api/chat/${agentId}/sessions`, { timeout: 60_000 });
  expect(res.ok()).toBeTruthy();
  return ((await res.json()) as { sessions: Array<{ session_id: string; turns: number; last_at: string }> }).sessions;
}

export interface EvaluatorRow {
  id: string;
  name?: string;
  level: string;
  source?: string;
  model_id?: string;
  [k: string]: unknown;
}

export async function evaluators(api: APIRequestContext): Promise<EvaluatorRow[]> {
  const res = await api.get("/api/eval/evaluators", { timeout: 60_000 });
  expect(res.ok()).toBeTruthy();
  return ((await res.json()) as { evaluators: EvaluatorRow[] }).evaluators;
}

export interface DatasetRow {
  id: string;
  name: string;
  items?: Array<Record<string, unknown>>;
  cloud?: Record<string, unknown>;
  [k: string]: unknown;
}

export async function datasets(api: APIRequestContext): Promise<DatasetRow[]> {
  const res = await api.get("/api/eval/datasets", { timeout: 60_000 });
  expect(res.ok()).toBeTruthy();
  return ((await res.json()) as { datasets: DatasetRow[] }).datasets;
}

/** Preset row of `/api/system-agents` by key. */
export async function preset(api: APIRequestContext, key: string): Promise<Record<string, unknown> | undefined> {
  const body = (await (await api.get("/api/system-agents", { timeout: 60_000 })).json()) as { presets?: Array<Record<string, unknown>> };
  return (body.presets ?? []).find((p) => p.key === key);
}

export const FIFTEEN_MIN = 15 * 60;

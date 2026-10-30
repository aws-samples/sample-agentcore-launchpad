import { expect, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";
import { attach, awsRead, evidence, ledger, listAgents, sleep } from "./helpers";

/**
 * N12 evaluation campaign helpers (REQUIREMENTS §4.1 cases 24–31 + plan §E.3 flows).
 *
 * Run the 2x-series specs with
 *   N10_PREFIX=n12e- N10_EVIDENCE_ROUND=round-n12 \
 *   N10_LEDGER=../../../.superpowers/sdd/n12-e2e/progress.md npx playwright test cn/4[0-8]-*.spec.ts
 * Everything the campaign creates is named `n12e-…` and is removed by `cn/98-n12e-cleanup.spec.ts`.
 */
export const N12E_PREFIX = "n12e-";
export const isN12E = (name: unknown) => String(name ?? "").startsWith(N12E_PREFIX);

/** Evaluator names must match ^[a-zA-Z][a-zA-Z0-9_]{0,47}$ — no dashes, so the campaign prefix becomes `n12e_`. */
export const N12E_EVALUATOR_PREFIX = "n12e_";

export const WORKER_PRESET_KEY = "eval-worker";
export const WORKER_RUNTIME_ID = "launchpad_eval_worker_t1_a1b2c3-EXAMPLE002";
export const JUDGE_DEFAULT = "global.zai.glm-5.3";
export const JUDGE_ALT = "global.moonshotai.kimi-k3";

/** The one active `n12e-smoke-…` Strands HTTP agent of this campaign (newest first). */
export async function smokeAgent(api: APIRequestContext): Promise<{ id: string; name: string; arn: string; resource_id: string }> {
  const rows = (await listAgents(api)).filter((a) => String(a.name).startsWith(`${N12E_PREFIX}smoke-`) && a.status === "active");
  rows.sort((a, b) => String(b.name).localeCompare(String(a.name)));
  expect(rows[0], "an active n12e-smoke-… agent (run cn/40-n12e-smoke-agent.spec.ts first)").toBeTruthy();
  const res = await api.get(`/api/agents/${rows[0].id}`);
  const full = (await res.json()) as Record<string, unknown>;
  return { id: String(full.id), name: String(full.name), arn: String(full.arn), resource_id: String(full.resource_id) };
}

export interface EvalRun {
  id: string;
  status: string;
  progress?: string | null;
  scores?: unknown;
  [k: string]: unknown;
}

/**
 * Poll GET /api/eval/runs/{id} until the run leaves the live states. Logs every status /
 * progress transition to the ledger (the worker's progress line is the §E.3 flow-11 evidence).
 */
export async function waitForRun(
  api: APIRequestContext,
  runId: string,
  opts: { timeoutMs?: number; intervalMs?: number; label?: string; onTick?: (r: EvalRun) => void } = {},
): Promise<{ run: EvalRun; seconds: number; transitions: string[] }> {
  const timeoutMs = opts.timeoutMs ?? 30 * 60 * 1000;
  const intervalMs = opts.intervalMs ?? 10_000;
  const started = Date.now();
  const transitions: string[] = [];
  let lastKey = "";
  let last: EvalRun | undefined;
  while (Date.now() - started < timeoutMs) {
    const res = await api.get(`/api/eval/runs/${runId}`);
    if (res.ok()) {
      last = (await res.json()) as EvalRun;
      const key = `${last.status}|${String(last.progress ?? "")}`.slice(0, 160);
      if (key !== lastKey) {
        lastKey = key;
        const line = `${opts.label ?? "run"} ${runId} +${Math.round((Date.now() - started) / 1000)}s ${key}`;
        transitions.push(line);
        ledger(line);
      }
      opts.onTick?.(last);
      const st = String(last.status).toLowerCase();
      if (!["queued", "pending", "running", "invoking", "waiting", "evaluating", "submitting", "replaying"].includes(st)) {
        return { run: last, seconds: Math.round((Date.now() - started) / 1000), transitions };
      }
    }
    await sleep(intervalMs);
  }
  throw new Error(`run ${runId} still ${last?.status ?? "unknown"} after ${Math.round(timeoutMs / 1000)}s`);
}

/** Generic poller over any GET endpoint until `done(body)` is true. */
export async function pollUntil<T>(
  api: APIRequestContext,
  path: string,
  done: (body: T) => boolean,
  opts: { timeoutMs?: number; intervalMs?: number; label?: string; key?: (b: T) => string } = {},
): Promise<{ body: T; seconds: number }> {
  const timeoutMs = opts.timeoutMs ?? 10 * 60 * 1000;
  const intervalMs = opts.intervalMs ?? 10_000;
  const started = Date.now();
  let lastKey = "";
  let last: T | undefined;
  while (Date.now() - started < timeoutMs) {
    const res = await api.get(path);
    if (res.ok()) {
      last = (await res.json()) as T;
      if (opts.key) {
        const k = opts.key(last);
        if (k !== lastKey) {
          lastKey = k;
          ledger(`${opts.label ?? path} +${Math.round((Date.now() - started) / 1000)}s ${k.slice(0, 200)}`);
        }
      }
      if (done(last)) return { body: last, seconds: Math.round((Date.now() - started) / 1000) };
    }
    await sleep(intervalMs);
  }
  throw new Error(`${opts.label ?? path} not settled after ${Math.round(timeoutMs / 1000)}s (last=${JSON.stringify(last).slice(0, 300)})`);
}

/** Screenshot + ledger line in one call (evidence files are `caseNN-<name>.png`). */
export async function snap(page: Page, name: string, note?: string): Promise<void> {
  const file = await evidence(page, name);
  ledger(`evidence ${file.split("/").pop()}${note ? ` — ${note}` : ""}`);
}

/** Attach a JSON body and ledger a one-line summary. */
export async function record(testInfo: TestInfo, name: string, body: unknown, summary?: string): Promise<void> {
  await attach(testInfo, name, body);
  if (summary) ledger(`${name}: ${summary}`);
}

export function runtimeEndpoints(runtimeId: string): Array<{ name: string; status: string; liveVersion?: string }> {
  const raw = awsRead(`bedrock-agentcore-control list-agent-runtime-endpoints --agent-runtime-id ${runtimeId}`);
  try {
    return ((JSON.parse(raw) as { runtimeEndpoints?: Array<{ name: string; status: string; liveVersion?: string }> }).runtimeEndpoints ?? []);
  } catch {
    return [];
  }
}

/**
 * V2 dropdowns are a trigger button (`data-testid`) opening a `role=listbox` with
 * `role=option` rows (`frontend/src/v2/ui.tsx::useListbox`). Pick one by label.
 */
export async function pickOption(page: Page, triggerTestId: string, label: RegExp | string): Promise<void> {
  await page.getByTestId(triggerTestId).click();
  const list = page.getByRole("listbox");
  await expect(list).toBeVisible();
  await list.getByRole("option", { name: label }).first().click();
}

export interface DatasetRow {
  id: string;
  name: string;
  items?: Array<Record<string, unknown>>;
  [k: string]: unknown;
}

export async function datasets(api: APIRequestContext): Promise<DatasetRow[]> {
  const res = await api.get("/api/eval/datasets");
  expect(res.ok()).toBeTruthy();
  return ((await res.json()) as { datasets: DatasetRow[] }).datasets;
}

/** Newest `n12e-ds-…` local dataset (created by spec 42). */
export async function campaignDataset(api: APIRequestContext): Promise<DatasetRow> {
  const rows = (await datasets(api)).filter((d) => String(d.name).startsWith(`${N12E_PREFIX}ds-`));
  rows.sort((a, b) => String(b.name).localeCompare(String(a.name)));
  expect(rows[0], "an n12e-ds-… dataset (run cn/42-dataset.spec.ts first)").toBeTruthy();
  return rows[0];
}

export interface EvaluatorRow {
  id: string;
  name?: string;
  level: string;
  source?: string;
  definition?: string;
  model_id?: string;
  [k: string]: unknown;
}

export async function evaluators(api: APIRequestContext): Promise<EvaluatorRow[]> {
  const res = await api.get("/api/eval/evaluators");
  expect(res.ok()).toBeTruthy();
  return ((await res.json()) as { evaluators: EvaluatorRow[] }).evaluators;
}

/** The custom judge `n12e_cn_tone` created by spec 41 (its id is `cust_<hex12>`). */
export async function campaignJudge(api: APIRequestContext): Promise<EvaluatorRow | undefined> {
  return (await evaluators(api)).find((e) => e.name === `${N12E_EVALUATOR_PREFIX}cn_tone`);
}

/** Chat sessions of an agent as the console lists them (newest first). */
export async function chatSessions(api: APIRequestContext, agentId: string): Promise<Array<{ session_id: string; turns: number; last_at: string }>> {
  const res = await api.get(`/api/chat/${agentId}/sessions`);
  expect(res.ok()).toBeTruthy();
  return ((await res.json()) as { sessions: Array<{ session_id: string; turns: number; last_at: string }> }).sessions;
}

export interface TaskWizardOpts {
  name: string;
  agentName: string;
  mode?: "evaluators" | "insights";
  /** `dataset` replays the dataset against the agent; `window` evaluates the agent's recent sessions (no replay) */
  source: "dataset" | "window";
  datasetName?: string;
  evaluatorIds?: string[];
  evidencePrefix: string;
}

/**
 * Drive the V2 two-step task wizard (`/v2/eval/tasks?view=new`) and return the new run id.
 * Throws with the wizard's alert text when the second step refuses the submission.
 */
export async function createTaskViaWizard(page: Page, o: TaskWizardOpts): Promise<string> {
  const api = page.request;
  await page.goto("/v2/eval/tasks?view=new");
  await page.getByTestId("v2-task-name").fill(o.name);
  await pickOption(page, "v2-task-agent", new RegExp(o.agentName));
  if (o.mode === "insights") await page.getByTestId("v2-task-mode-insights").click();
  if (o.source === "dataset") {
    await page.getByTestId("v2-task-src-dataset").click();
    await pickOption(page, "v2-task-dataset", new RegExp(o.datasetName!));
  } else {
    await page.getByTestId("v2-task-src-window").click();
  }
  await snap(page, `${o.evidencePrefix}-wizard-step1`);
  await page.getByTestId("v2-task-next").click();
  if (o.mode !== "insights" && o.evaluatorIds) {
    const preChecked = await page.evaluate(() =>
      Array.from(document.querySelectorAll<HTMLInputElement>('[data-testid^="v2-task-eval-"]')).filter((e) => e.checked).map((e) => e.dataset.testid!.replace("v2-task-eval-", "")),
    );
    for (const id of preChecked) if (!o.evaluatorIds.includes(id)) await page.getByTestId(`v2-task-eval-${id}`).click();
    for (const id of o.evaluatorIds) {
      const box = page.getByTestId(`v2-task-eval-${id}`);
      await expect(box, `evaluator ${id} offered`).toBeVisible();
      if (!(await box.isChecked().catch(() => false))) await box.click();
    }
  } else if (o.mode === "insights") {
    const boxes = page.locator('[data-testid^="v2-task-insight-"]');
    const n = await boxes.count();
    for (let i = 0; i < n; i++) if (!(await boxes.nth(i).isChecked().catch(() => false))) await boxes.nth(i).click();
  }
  await snap(page, `${o.evidencePrefix}-wizard-step2`);
  const before = new Set(((await (await api.get("/api/eval/runs")).json()).runs as Array<{ id: string }>).map((r) => r.id));
  await page.getByTestId("v2-task-submit").click();
  const alert = page.getByRole("alert");
  await Promise.race([
    expect(page).not.toHaveURL(/view=new/, { timeout: 60_000 }),
    expect(alert.first()).toBeVisible({ timeout: 60_000 }),
  ]).catch(() => {});
  if (/view=new/.test(page.url())) {
    const msg = (await alert.first().innerText().catch(() => "")).replace(/\s+/g, " ").trim();
    await snap(page, `${o.evidencePrefix}-wizard-refused`, msg.slice(0, 80));
    throw new Error(`task wizard refused the submission: ${msg.slice(0, 400)}`);
  }
  const runs = (await (await api.get("/api/eval/runs")).json()).runs as Array<Record<string, unknown>>;
  const run = runs.find((r) => !before.has(String(r.id)));
  expect(run, `new run for ${o.name}`).toBeTruthy();
  ledger(`${o.evidencePrefix} run ${run!.id} submitted status=${run!.status} mode=${run!.mode} source=${o.source}`);
  return String(run!.id);
}

/** Mask anything that looks like a key / token before it lands in evidence text. */
export function mask(s: string): string {
  return s.replace(/(lp_[A-Za-z0-9]{6})[A-Za-z0-9_-]{8,}/g, "$1…").replace(/(Bearer\s+)[^\s"]+/gi, "$1…");
}

export const SMOKE_PROMPTS = [
  "Reply with exactly: N12E-OK. Then say what 3+4 equals in one short sentence.",
  "Name two AWS regions in China in one sentence.",
  "In one sentence, what is Amazon Bedrock AgentCore Runtime?",
];

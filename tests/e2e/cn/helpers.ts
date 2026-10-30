import { expect, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

// Round 4 (N10 v4) by default; N10_EVIDENCE_ROUND=round-3 / round-2 / round-1 reproduce the earlier layouts.
export const EVIDENCE_ROUND = process.env.N10_EVIDENCE_ROUND ?? "round-4";
export const EVIDENCE_DIR = path.resolve(__dirname, `../../../docs/cn/evidence/${EVIDENCE_ROUND}`);
export const LEDGER = path.resolve(
  __dirname,
  process.env.N10_LEDGER ?? "../../../.superpowers/sdd/n10v4/progress.md",
);

export function ledger(line: string): void {
  fs.mkdirSync(path.dirname(LEDGER), { recursive: true });
  fs.appendFileSync(LEDGER, `${new Date().toISOString()} ${line}\n`);
}

/** Full-page evidence screenshot into docs/cn/evidence/round-1. Masks password fields. */
export async function evidence(page: Page, name: string): Promise<string> {
  fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
  const file = path.join(EVIDENCE_DIR, `${name}.png`);
  await page.screenshot({
    path: file,
    fullPage: true,
    mask: [page.locator('input[type="password"]')],
  });
  return file;
}

export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export interface JobView {
  job_id: string;
  status: string;
  [k: string]: unknown;
}

/** Poll GET /api/jobs/{id} until it leaves the running states. Returns the final job. */
export async function waitForJob(
  api: APIRequestContext,
  jobId: string,
  opts: { timeoutMs?: number; intervalMs?: number; onTick?: (j: JobView) => void } = {},
): Promise<JobView> {
  const timeoutMs = opts.timeoutMs ?? 25 * 60 * 1000;
  const intervalMs = opts.intervalMs ?? 15_000;
  const started = Date.now();
  let last: JobView | undefined;
  while (Date.now() - started < timeoutMs) {
    const res = await api.get(`/api/jobs/${jobId}`, { timeout: 60_000 }).catch(() => null); // N-SCN-obs11: a stalled GET is a missed poll
    if (res?.ok()) {
      last = (await res.json()) as JobView;
      opts.onTick?.(last);
      const st = String(last.status ?? "").toLowerCase();
      if (!["pending", "queued", "running", "in_progress"].includes(st)) return last;
    }
    await sleep(intervalMs);
  }
  throw new Error(`job ${jobId} still ${last?.status ?? "unknown"} after ${Math.round(timeoutMs / 1000)}s`);
}

export async function getAgent(api: APIRequestContext, agentId: string): Promise<Record<string, unknown>> {
  const res = await api.get(`/api/agents/${agentId}`);
  expect(res.ok(), `GET /api/agents/${agentId} → ${res.status()}`).toBeTruthy();
  return (await res.json()) as Record<string, unknown>;
}

export async function listAgents(api: APIRequestContext): Promise<Array<Record<string, unknown>>> {
  const res = await api.get("/api/agents");
  expect(res.ok()).toBeTruthy();
  const body = (await res.json()) as { agents: Array<Record<string, unknown>> };
  return body.agents;
}

export function attach(testInfo: TestInfo, name: string, body: unknown): Promise<void> {
  return testInfo.attach(name, {
    body: typeof body === "string" ? body : JSON.stringify(body, null, 2),
    contentType: typeof body === "string" ? "text/plain" : "application/json",
  });
}

// v4 test data is `e2e4-…` (v3 `e2e3-…`, v2 `e2e2-…`, v1 `e2e-…`); override with N10_PREFIX.
export const E2E_PREFIX = process.env.N10_PREFIX ?? "e2e4-";
export const isE2E = (name: unknown) => String(name).startsWith(E2E_PREFIX);
export function e2eName(suffix: string): string {
  // AgentSpec.name: ^[a-z][a-z0-9-]{2,47}$
  const stamp = new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "").toLowerCase();
  return `${E2E_PREFIX}${suffix}-${stamp}`.slice(0, 48).replace(/-+$/, "");
}

// ── shared flows ──────────────────────────────────────────────────────────

export interface HarnessWizardOpts {
  name: string;
  prompt: string;
  gateways?: string[];
  skills?: string[];
  kbs?: string[];
  longTerm?: boolean;
  evidencePrefix: string;
}

/** Drive the V2 wizard for 方式B (self-built Harness) and return the new agent id. */
export async function createHarnessViaWizard(page: Page, o: HarnessWizardOpts): Promise<string> {
  await page.goto("/v2/agents");
  await page.getByTestId("v2-agent-new").click();
  await page.getByTestId("v2-agent-method-harness").click();
  await page.getByTestId("v2-agent-wizard-next").click();
  await page.getByTestId("v2-agent-name").fill(o.name);
  await page.getByTestId("v2-agent-prompt").fill(o.prompt);
  for (const g of o.gateways ?? []) {
    const box = page.getByTestId("v2-agent-gateways").locator("label", { hasText: g }).locator('input[type="checkbox"]');
    await expect(box, `gateway target ${g} should be attachable`).toBeEnabled();
    await box.check();
  }
  for (const s of o.skills ?? []) {
    const box = page.getByTestId("v2-agent-skills").locator("label", { hasText: s }).locator('input[type="checkbox"]');
    await expect(box, `skill ${s} should be listed`).toBeVisible();
    await box.check();
  }
  for (const k of o.kbs ?? []) {
    const box = page.getByTestId("v2-agent-kbs").locator("label", { hasText: k }).locator('input[type="checkbox"]');
    await expect(box, `knowledge base ${k} should be listed`).toBeVisible();
    await box.check();
  }
  if (o.longTerm) await page.getByTestId("v2-agent-long-term").check();
  await evidence(page, `${o.evidencePrefix}-wizard-configure`);
  await page.getByTestId("v2-agent-wizard-next").click();
  await expect(page.getByTestId("v2-agent-review")).toBeVisible();
  await evidence(page, `${o.evidencePrefix}-wizard-review`);
  await page.getByTestId("v2-agent-wizard-submit").click();
  await expect(page).toHaveURL(/view=detail&id=/, { timeout: 60_000 });
  return new URL(page.url()).searchParams.get("id")!;
}

export interface DeployResult {
  job: JobView;
  agent: Record<string, unknown>;
  seconds: number;
}

/** Poll the latest deployment job of `agentId`; screenshot the stage board; return the outcome. */
export async function waitForDeploy(
  page: Page,
  testInfo: TestInfo,
  agentId: string,
  label: string,
  startedAt = Date.now(),
): Promise<DeployResult> {
  let agent = await getAgent(page.request, agentId);
  const deployments = (agent.deployments as Array<{ job_id: string }>) ?? [];
  const jobId = deployments[0]?.job_id;
  expect(jobId, "deployment job id").toBeTruthy();
  let lastKey = "";
  const job = await waitForJob(page.request, jobId, {
    onTick: (j) => {
      const ev = (j.events as Array<{ stage: string; msg: string }>) ?? [];
      const tail = ev[ev.length - 1];
      const key = tail ? `${tail.stage}:${tail.msg}` : "";
      if (key !== lastKey) {
        lastKey = key;
        ledger(`${label} job ${jobId.slice(0, 8)} ${j.status} ${key.slice(0, 140)}`);
      }
    },
  });
  const seconds = Math.round((Date.now() - startedAt) / 1000);
  await attach(testInfo, `${label}-deploy-job.json`, job);
  agent = await getAgent(page.request, agentId);
  ledger(`${label} deploy ${job.status} in ${seconds}s agent.status=${agent.status}`);
  await page.goto(`/v2/agents?view=detail&id=${encodeURIComponent(agentId)}`);
  await expect(page.getByTestId("v2-agent-log")).toBeVisible();
  await evidence(page, `${label}-deploy-detail`);
  return { job, agent, seconds };
}

export interface ChatResult {
  answer: string;
  sessionId: string;
  toolCalls: number;
  toolNames: string[];
  /** error bubbles rendered in the thread after this turn (0 = clean turn) */
  errors: number;
  seconds: number;
}

/** Send one prompt in the V2 Chat Playground and wait for the streamed answer to settle. */
/**
 * N-SCN-obs18 (test-side): an error row whose text is an UPSTREAM transient 5xx — the runtime relaying Bedrock's
 * `internalServerException` / `ServiceUnavailable` / throttling — is retried ONCE after a short wait, in the same
 * session, with an `observation:` ledger line. Never on 4xx, policy denials (`policy.denied`, PolicyDenyCard) or any
 * other product error text — those stay the turn's answer so the caller's assertions see them.
 */
export const UPSTREAM_TRANSIENT_RE =
  /internalServerException|ServiceUnavailable(Exception)?|Throttling(Exception)?|Try your request again|too many requests|Read ?timeout|ReadTimeoutError|timed out|\b50[234]\b/i;
const NEVER_RETRY_RE = /policy\.denied|PolicyDeny|forbidden|\b40\d\b|denied by policy/i;
const RETRY_WAIT_MS = 10_000;

export function isUpstreamTransient(answer: string): boolean {
  return UPSTREAM_TRANSIENT_RE.test(answer) && !NEVER_RETRY_RE.test(answer);
}

export async function chatOnce(
  page: Page,
  agentId: string,
  prompt: string,
  o: { sessionId?: string; timeoutMs?: number; noRetry?: boolean } = {},
): Promise<ChatResult> {
  const url = `/v2/chat?agent=${encodeURIComponent(agentId)}${o.sessionId ? `&session=${encodeURIComponent(o.sessionId)}` : ""}`;
  if (!page.url().endsWith(url)) await page.goto(url);
  await expect(page.getByTestId("chat-input")).toBeVisible();
  const bubbles = page.locator(".v2-chat-msg.agent .v2-chat-bub");
  const before = await bubbles.count();
  await page.getByTestId("chat-input").fill(prompt);
  const t0 = Date.now();
  await page.getByTestId("chat-send").click();
  const timeout = o.timeoutMs ?? 5 * 60 * 1000;
  // a turn ends either with a new agent bubble or with an error row (e.g. the runtime's MaxTokensReachedException,
  // N-SCN-bug4) — wait for whichever comes first instead of spending the whole timeout on a bubble that never comes
  const errorRows = page.locator(".v2-chat-msg.error, .v2-chat-error");
  const errorsBefore = await errorRows.count();
  await expect
    .poll(async () => (await bubbles.count()) > before || (await errorRows.count()) > errorsBefore, { timeout, message: "agent bubble or error row" })
    .toBe(true);
  const errors = await errorRows.count();
  if (errors > errorsBefore && (await bubbles.count()) === before) {
    // surface the product's error text as the answer so the caller's `errors === 0` / looksLikeError assertions fail fast
    const answer = (await errorRows.last().innerText()).trim();
    await expect(page).toHaveURL(/session=/);
    const sessionId = new URL(page.url()).searchParams.get("session")!;
    if (!o.noRetry && isUpstreamTransient(answer)) {
      // N-SCN-obs18: one retry of the same prompt in the same session after a short wait; recorded, never graded
      ledger(`observation: N-SCN-obs18 chatOnce retrying once after an upstream transient error row (${RETRY_WAIT_MS / 1000}s wait) — ${answer.slice(0, 160).replace(/\n/g, " ")}`);
      await sleep(RETRY_WAIT_MS);
      const again = await chatOnce(page, agentId, prompt, { ...o, sessionId, noRetry: true });
      return { ...again, seconds: Math.round((Date.now() - t0) / 1000) };
    }
    const toolNames = (await page.getByTestId("tool-call").locator(".mono").allInnerTexts()).map((t) => t.trim());
    return { answer, sessionId, toolCalls: toolNames.length, toolNames, errors, seconds: Math.round((Date.now() - t0) / 1000) };
  }
  await expect(page.locator(".v2-chat-msg.agent").last().getByText(/streaming/i)).toHaveCount(0, { timeout });
  // the input is cleared and re-enabled once the stream is done (Send stays disabled on an empty composer)
  await expect(page.getByTestId("chat-input")).toBeEnabled({ timeout });
  const answer = (await bubbles.last().innerText()).trim();
  await expect(page).toHaveURL(/session=/);
  const sessionId = new URL(page.url()).searchParams.get("session")!;
  const toolNames = (await page.getByTestId("tool-call").locator(".mono").allInnerTexts()).map((t) => t.trim());
  return { answer, sessionId, toolCalls: toolNames.length, toolNames, errors, seconds: Math.round((Date.now() - t0) / 1000) };
}

/** A turn that errored on the product side (runtime error bubble / stream error / echo fallback). */
export function looksLikeError(answer: string): boolean {
  // anchored alternation (v2's `/^\[echo|error|…/` matched "error" anywhere, e.g. "error_rate" in a model answer)
  return /^\s*(\[echo|error|exception|traceback|stopped generating|maximum token)/i.test(answer);
}

export const REPO_ROOT = path.resolve(__dirname, "../../..");

/**
 * Walk a registry record through the approval lifecycle in the UI
 * (Draft → Submit for approval → Approve) and return its final status.
 */
export async function approveRecordViaUi(page: Page, recordId: string): Promise<string> {
  await page.goto(`/v2/registry?view=detail&id=${encodeURIComponent(recordId)}`);
  const submit = page.getByTestId("v2-registry-submit");
  const approve = page.getByTestId("v2-registry-approve");
  await expect(submit.or(approve).first()).toBeVisible({ timeout: 30_000 });
  if (await submit.isVisible()) {
    await submit.click();
    const ok = page.getByTestId("v2-confirm-ok");
    if (await ok.isVisible({ timeout: 1500 }).catch(() => false)) await ok.click();
    await expect(approve).toBeVisible({ timeout: 30_000 });
  }
  await approve.click();
  const ok2 = page.getByTestId("v2-confirm-ok");
  if (await ok2.isVisible({ timeout: 1500 }).catch(() => false)) await ok2.click();
  let status = "";
  for (let i = 0; i < 20; i++) {
    const rec = await (await page.request.get(`/api/registry/records/${recordId}`)).json();
    status = String(rec.status);
    if (status === "APPROVED") break;
    await sleep(1500);
  }
  return status;
}

/**
 * Precondition for gateway attachment (N10-bug1): bootstrap leaves the shared
 * gateway's targets unregistered in the local registry, so the wizard offers
 * "No approved gateway targets". The product exposes POST /api/registry/sync-defaults
 * (no console button) to import them; the records then need approval in the UI.
 */
export async function ensureGatewayTargetApproved(page: Page, target: string): Promise<string> {
  let records = (await (await page.request.get("/api/registry/records")).json()).records as Array<{ record_id: string; name: string; status: string }>;
  let rec = records.find((r) => r.name === target);
  if (!rec) {
    const res = await page.request.post("/api/registry/sync-defaults");
    ledger(`sync-defaults → ${res.status()} ${(await res.text()).slice(0, 300)}`);
    records = (await (await page.request.get("/api/registry/records")).json()).records;
    rec = records.find((r) => r.name === target);
  }
  if (!rec) throw new Error(`gateway target ${target} has no registry record even after sync-defaults`);
  if (rec.status !== "APPROVED") {
    const st = await approveRecordViaUi(page, rec.record_id);
    ledger(`registry record ${target} (${rec.record_id}) → ${st}`);
    await evidence(page, `case05-registry-${target}-approved`);
  }
  return rec.record_id;
}

/**
 * N10_REUSE=1: reuse an already-active `e2e-<prefix>-…` agent from an earlier run of the
 * same case instead of deploying a fresh one (the deploy evidence was captured then).
 */
export async function reusableAgent(api: APIRequestContext, prefix: string): Promise<{ id: string; name: string } | null> {
  if (!process.env.N10_REUSE) return null;
  const rows = (await listAgents(api)).filter((a) => String(a.name).startsWith(`${E2E_PREFIX}${prefix}-`) && a.status === "active");
  rows.sort((a, b) => String(b.name).localeCompare(String(a.name)));
  return rows[0] ? { id: String(rows[0].id), name: String(rows[0].name) } : null;
}

// ── v2 additions (REQUIREMENTS §4.1) ──────────────────────────────────────

/** Switch the console language through the V2 top-bar toggle (persisted by i18next in localStorage). */
export async function setLanguage(page: Page, lang: "en" | "zh-CN"): Promise<void> {
  // localStorage is unreachable on about:blank — land on the console first
  if (!/^https?:/.test(page.url())) await page.goto("/v2");
  await page.evaluate((l) => localStorage.setItem("i18nextLng", l), lang);
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("lang", lang === "en" ? /^en/ : /^zh/, { timeout: 30_000 }).catch(() => {});
}

/**
 * §4.1 case 1: visible text nodes that look like a raw i18n key
 * (`^[a-z0-9_.]+\.[a-z0-9_.]+$`). Excludes code/pre/input values (ARNs, ids, model ids
 * are rendered in <code>/.mono and are legitimately dotted).
 */
export async function rawI18nKeys(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const re = /^[a-z0-9_.]+\.[a-z0-9_.]+$/;
    const out: string[] = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let n: Node | null;
    while ((n = walker.nextNode())) {
      const text = (n.textContent ?? "").trim();
      if (!text || !re.test(text)) continue;
      const el = n.parentElement;
      if (!el) continue;
      if (el.closest("code, pre, script, style, .mono, textarea, kbd")) continue;
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden") continue;
      if (/^\d+(\.\d+)+$/.test(text)) continue; // version numbers / decimals
      out.push(text);
    }
    return Array.from(new Set(out));
  });
}

/** Log in through the real form in a *fresh* context (no storage state) and return the page. */
export async function loginFresh(
  browser: import("@playwright/test").Browser,
  username: string,
  password: string,
  baseURL: string,
): Promise<{ page: Page; context: import("@playwright/test").BrowserContext }> {
  // NB: browser.newContext() inherits the project storageState (the admin session) unless overridden
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] }, ignoreHTTPSErrors: true, baseURL, locale: "en-US", viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await page.goto("/");
  await page.locator('input[autocomplete="username"]').fill(username);
  await page.locator('input[type="password"][autocomplete="current-password"]').fill(password);
  await page.locator('input[type="password"][autocomplete="current-password"]').press("Enter");
  // the V2 shell renders its sidebar as <aside aria-label="Console navigation"> (the top-bar product tab, the last <nav>, went with 262ca426); the classic console keeps <nav>
  await expect(page.locator("nav, aside[aria-label]").first()).toBeVisible({ timeout: 60_000 });
  return { page, context };
}

export interface StrandsWizardOpts {
  name: string;
  prompt: string;
  protocol?: "http" | "a2a";
  a2aSkill?: { name: string; description: string; tags: string };
  evidencePrefix: string;
}

/** Drive the V2 wizard for the Strands (zip_runtime) method; `protocol: "a2a"` publishes an A2A server. */
export async function createStrandsViaWizard(page: Page, o: StrandsWizardOpts): Promise<string> {
  await page.goto("/v2/agents");
  await page.getByTestId("v2-agent-new").click();
  await page.getByTestId("v2-agent-method-zip_runtime").click();
  await page.getByTestId("v2-agent-wizard-next").click();
  await page.getByTestId("v2-agent-name").fill(o.name);
  await page.getByTestId("v2-agent-prompt").fill(o.prompt);
  if (o.protocol === "a2a") {
    await page.getByTestId("v2-agent-protocol-a2a").click();
    await expect(page.getByTestId("v2-agent-a2a-skills")).toBeVisible();
    if (o.a2aSkill) {
      const first = page.getByTestId("v2-agent-a2a-name-0");
      await first.fill(o.a2aSkill.name);
      const row = first.locator("xpath=..");
      await row.locator("input").nth(1).fill(o.a2aSkill.description);
      await row.locator("input").nth(2).fill(o.a2aSkill.tags);
    }
  }
  await evidence(page, `${o.evidencePrefix}-wizard-configure`);
  await page.getByTestId("v2-agent-wizard-next").click();
  await expect(page.getByTestId("v2-agent-review")).toBeVisible();
  await evidence(page, `${o.evidencePrefix}-wizard-review`);
  await page.getByTestId("v2-agent-wizard-submit").click();
  await expect(page).toHaveURL(/view=detail&id=/, { timeout: 60_000 });
  return new URL(page.url()).searchParams.get("id")!;
}

/**
 * §4.1 case 2: the Chat memory rail must show ≥ 1 short-term event after the turn
 * WITHOUT a reload. Polls the rail DOM only (never navigates).
 */
export async function memoryRailEvents(page: Page, timeoutMs = 90_000): Promise<number> {
  const started = Date.now();
  let last = 0;
  while (Date.now() - started < timeoutMs) {
    const txt = (await page.getByTestId("memory-panel").innerText().catch(() => "")) ?? "";
    const m = /Short-term events\s*(\d+)|短期事件\s*(\d+)/i.exec(txt.replace(/\n/g, " "));
    last = m ? Number(m[1] ?? m[2]) : last;
    if (last > 0) return last;
    await sleep(3000);
  }
  return last;
}

/** Read-only AWS probe through the CN profile (describe/get/list only). Returns stdout or "" on error. */
export function awsRead(args: string): string {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { execSync } = require("node:child_process") as typeof import("node:child_process");
  try {
    return execSync(`aws ${args} --profile cn-northwest-1 --region cn-northwest-1 --output json`, {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "pipe"],
      timeout: 120_000,
    });
  } catch (err) {
    return `ERROR ${(err as { stderr?: string }).stderr ?? String(err)}`.slice(0, 500);
  }
}

export async function registryRecords(api: APIRequestContext): Promise<Array<{ record_id: string; name: string; type: string; status: string }>> {
  return ((await (await api.get("/api/registry/records")).json()).records ?? []) as Array<{ record_id: string; name: string; type: string; status: string }>;
}

/** Active agent of this run by prefix (e.g. "harness"), newest first. */
export async function activeAgent(api: APIRequestContext, prefix: string): Promise<{ id: string; name: string } | null> {
  const rows = (await listAgents(api)).filter((a) => String(a.name).startsWith(`${E2E_PREFIX}${prefix}-`) && a.status === "active");
  rows.sort((a, b) => String(b.name).localeCompare(String(a.name)));
  return rows[0] ? { id: String(rows[0].id), name: String(rows[0].name) } : null;
}

// ── v4 additions (round-4 re-check) ───────────────────────────────────────

/** Small JSON state shared between spec files of one run (git-ignored, next to the auth state). */
const STATE_FILE = path.resolve(__dirname, "../.auth/n10v4-state.json");
export function stateGet<T = unknown>(key: string): T | undefined {
  try {
    return (JSON.parse(fs.readFileSync(STATE_FILE, "utf8")) as Record<string, T>)[key];
  } catch {
    return undefined;
  }
}
export function stateSet(key: string, value: unknown): void {
  let cur: Record<string, unknown> = {};
  try {
    cur = JSON.parse(fs.readFileSync(STATE_FILE, "utf8")) as Record<string, unknown>;
  } catch {
    /* first write */
  }
  cur[key] = value;
  fs.mkdirSync(path.dirname(STATE_FILE), { recursive: true });
  fs.writeFileSync(STATE_FILE, JSON.stringify(cur, null, 2));
}

export interface ByocWizardOpts {
  name: string;
  zip: string;
  entrypoint?: string;
  evidencePrefix: string;
}

/** Drive the V2 wizard for a BYOC code zip and return the new agent id. */
export async function createByocViaWizard(page: Page, o: ByocWizardOpts): Promise<string> {
  await page.goto("/v2/agents");
  await page.getByTestId("v2-agent-new").click();
  await page.getByTestId("v2-agent-method-byoc").click();
  await page.getByTestId("v2-agent-wizard-next").click();
  await page.getByTestId("v2-agent-name").fill(o.name);
  await page.getByTestId("v2-agent-byoc-kind-code_zip").click();
  await page.getByTestId("v2-agent-byoc-file").setInputFiles(o.zip);
  await expect(page.getByTestId("v2-agent-byoc-uploaded")).toBeVisible({ timeout: 120_000 });
  await expect(page.getByTestId("v2-agent-byoc-uploaded")).toHaveText(new RegExp(path.basename(o.zip).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  const want = o.entrypoint ?? "main.py";
  const entry = page.getByTestId("v2-agent-byoc-entrypoint");
  if ((await entry.evaluate((el) => el.tagName)) === "INPUT") {
    await entry.fill(want);
  } else if (!new RegExp(want.replace(".", "\\.")).test(await entry.innerText())) {
    await entry.click();
    await page.getByRole("option", { name: want }).click();
  }
  await expect(page.getByTestId("v2-agent-byoc-models")).toBeVisible();
  await evidence(page, `${o.evidencePrefix}-byoc-configure`);
  await page.getByTestId("v2-agent-wizard-next").click();
  await expect(page.getByTestId("v2-agent-review")).toBeVisible();
  await evidence(page, `${o.evidencePrefix}-byoc-review`);
  await page.getByTestId("v2-agent-wizard-submit").click();
  await expect(page).toHaveURL(/view=detail&id=/, { timeout: 60_000 });
  return new URL(page.url()).searchParams.get("id")!;
}

export interface ClassicPresetsOpen {
  /** ms until the presets panel (any install-/uninstall- button or the panel root) rendered; -1 = never */
  readyMs: number;
  /** AuthGate spinner seen (`data-testid=v2-auth-loading`) and its `data-reason` samples with timings */
  stall: Array<{ atMs: number; reason: string | null }>;
  reloads: number;
}

/**
 * Open the `/agents/new` System-presets panel (kept after r13-a retired the classic console —
 * it has no V2 twin yet) and attribute a stuck full-page
 * spinner (round-3: the panel never mounted after a reload in the e2e browser; round-4
 * fix adds `data-testid=v2-auth-loading` + `data-reason=auth|workspace`). Samples the
 * spinner every 5 s, reloads once after `reloadAfterMs`, screenshots the stall.
 */
export async function openClassicPresets(page: Page, label: string, o: { timeoutMs?: number; reloadAfterMs?: number } = {}): Promise<ClassicPresetsOpen> {
  const timeoutMs = o.timeoutMs ?? 120_000;
  const reloadAfterMs = o.reloadAfterMs ?? 45_000;
  const t0 = Date.now();
  const out: ClassicPresetsOpen = { readyMs: -1, stall: [], reloads: 0 };
  await page.goto("/agents/new");
  const panel = page.locator('[data-testid^="install-"], [data-testid^="uninstall-"], [data-testid^="repair-"], [data-testid="preset-release"]').first();
  const spinner = page.getByTestId("v2-auth-loading");
  let reloaded = false;
  while (Date.now() - t0 < timeoutMs) {
    if (await panel.isVisible().catch(() => false)) {
      out.readyMs = Date.now() - t0;
      break;
    }
    if (await spinner.isVisible().catch(() => false)) {
      const reason = await spinner.getAttribute("data-reason").catch(() => null);
      out.stall.push({ atMs: Date.now() - t0, reason });
      if (out.stall.length === 1 || out.stall.length % 6 === 0) {
        await evidence(page, `${label}-authgate-stall-${out.stall.length}`);
        ledger(`${label} /agents/new AuthGate spinner visible at +${Math.round((Date.now() - t0) / 1000)}s data-reason=${reason}`);
      }
    }
    if (!reloaded && Date.now() - t0 > reloadAfterMs) {
      reloaded = true;
      out.reloads += 1;
      ledger(`${label} /agents/new still not mounted after ${Math.round(reloadAfterMs / 1000)}s — one reload`);
      await page.reload();
    }
    await sleep(5000);
  }
  ledger(`${label} /agents/new presets panel ready=${out.readyMs >= 0 ? `${out.readyMs}ms` : "NEVER"} spinnerSamples=${out.stall.length} reasons=${JSON.stringify([...new Set(out.stall.map((s) => s.reason))])} reloads=${out.reloads}`);
  return out;
}

/** Parse the shared `[skilleval] rollout finished: N ok, M errored[, K invalid]` lines of a Skill Lab job log. */
export function rolloutHealth(logText: string): { batches: number; ok: number; errored: number; invalid: number; lines: string[] } {
  const lines = [...logText.matchAll(/rollout finished: (\d+) ok, (\d+) errored(?:, (\d+) invalid)?/g)];
  return {
    batches: lines.length,
    ok: lines.reduce((n, m) => n + Number(m[1]), 0),
    errored: lines.reduce((n, m) => n + Number(m[2]), 0),
    invalid: lines.reduce((n, m) => n + Number(m[3] ?? 0), 0),
    lines: lines.map((m) => m[0]),
  };
}

/**
 * Shared runtime helpers of the round-16 combination suites (wizard pairwise, page pairwise,
 * UI random walk). Credentials: `LAUNCHPAD_E2E_PASSWORD` (admin) and
 * `LAUNCHPAD_E2E_MEMBER_PASSWORD` (the x16n-member fixture) are read from the environment
 * only — the sign-in goes through `POST /api/auth/login` on the browser context's own
 * request client (the cookie jar is shared, nothing is typed into a page, no storageState
 * on disk). Evidence bodies pass `redact()`; a screenshot is refused while a password field
 * holds text.
 */
import { type APIRequestContext, type Browser, type BrowserContext, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

/** The console under test — REQUIRED (no default: a stray local run must never target a shared host). */
export const BASE = (() => {
  const url = process.env.LAUNCHPAD_E2E_BASE_URL;
  if (!url) throw new Error("LAUNCHPAD_E2E_BASE_URL is not set — name the console under test explicitly (e.g. https://<t1>:8443)");
  return url.replace(/\/+$/, "");
})();
export const OUT = process.env.X16N_OUT ?? "/home/ubuntu/designs/combinations-20261019";
export const SHOTS = path.join(OUT, "screenshots");
export const EVIDENCE = path.join(OUT, "evidence");
for (const d of [SHOTS, EVIDENCE]) fs.mkdirSync(d, { recursive: true });

/** every fixture this round creates carries this prefix (cleanup sweeps on it) */
export const PREFIX = "x16n-";
export const MEMBER = "x16n-member";
/** a system preset's name: `POST /api/agents` answers 409 agent.name_reserved AFTER the
 *  whole spec validated and BEFORE any row / job — the deploy-free "would the server accept
 *  this spec" probe (backend/app/routers/agents.py create_agent order) */
export const RESERVED_NAME = "aws-agent-solution-architect";

/* ───────────── redaction / evidence ───────────── */
const SECRET_RE = /password|passwd|secret|api[_-]?key|(^|[^_a-z])token|^key$|session_secret|cookie/i;
/** keys that merely contain "token(s)" as a size knob are data, not credentials */
const NOT_SECRET_RE = /^(max_?tokens|next_token|tokens|input_tokens|output_tokens|total_tokens)$/i;
export function redact<T>(obj: T): T {
  if (Array.isArray(obj)) return obj.map(redact) as unknown as T;
  if (obj && typeof obj === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(obj as Record<string, unknown>)) out[k] = !NOT_SECRET_RE.test(k) && SECRET_RE.test(k) ? "[redacted]" : redact(v);
    return out as T;
  }
  if (typeof obj === "string" && /^lp_live_[0-9a-f]{8,}/.test(obj)) return "[redacted]" as unknown as T;
  return obj;
}
export function writeJson(name: string, data: unknown): string {
  const text = `${JSON.stringify(redact(data), null, 2)}\n`;
  if (/"(password|passwd|secret|api_key|apikey)"\s*:\s*"(?!\[redacted\])/i.test(text)) throw new Error(`credential-like key in evidence ${name}`);
  const file = path.join(EVIDENCE, name);
  fs.writeFileSync(file, text);
  return file;
}
const LOG = path.join(EVIDENCE, "run-log.txt");
export function log(...parts: unknown[]): void {
  const line = `${new Date().toISOString()} ${parts.map((p) => (typeof p === "string" ? p : JSON.stringify(redact(p)))).join(" ")}`;
  console.log(line);
  fs.appendFileSync(LOG, `${line}\n`);
}

export type Cls = "DEAD" | "BROKEN" | "INCONSISTENT" | "UX" | "FLAKY" | "KNOWN";
export interface Result {
  suite: string;
  id: string;
  verdict: "PASS" | "FAIL" | "BLOCKED" | "INFO";
  cls?: Cls;
  role?: string;
  combo?: Record<string, string>;
  expected?: string;
  observed?: string;
  repro?: unknown;
  screenshots?: string[];
  at?: string;
}
const RESULTS = path.join(EVIDENCE, "results.jsonl");
export function record(r: Result): void {
  const row = { ...redact(r), at: new Date().toISOString() };
  fs.appendFileSync(RESULTS, `${JSON.stringify(row)}\n`);
  log(`[${r.verdict}${r.cls ? `/${r.cls}` : ""}] ${r.suite}:${r.id}`, r.observed ?? "");
}
export function loadResults(): Result[] {
  if (!fs.existsSync(RESULTS)) return [];
  return fs.readFileSync(RESULTS, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l) as Result);
}

/* ───────────── sessions ───────────── */
export interface ApiHit {
  at: string;
  method: string;
  path: string;
  status: number;
  /** request body of a mutation (redacted) */
  req?: unknown;
  /** response body summary (redacted) */
  body?: unknown;
}
export interface Session {
  role: "admin" | "member" | "anon";
  username: string | null;
  ctx: BrowserContext;
  page: Page;
  api: Api;
  hits: ApiHit[];
  consoleErrors: { at: string; url: string; text: string }[];
  pageErrors: { at: string; url: string; text: string }[];
  failed: { url: string; error: string | undefined }[];
}

function summarize(body: unknown, depth = 0): unknown {
  if (body == null) return body;
  if (Array.isArray(body)) return { _len: body.length, _sample: body.slice(0, 8).map((x) => (x && typeof x === "object" ? pick(x as Record<string, unknown>) : x)) };
  if (typeof body === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(body as Record<string, unknown>)) {
      if (Array.isArray(v)) out[k] = summarize(v, depth + 1);
      else if (v && typeof v === "object" && depth < 2) out[k] = summarize(v, depth + 1);
      else if (typeof v === "string" && v.length > 200) out[k] = `${v.slice(0, 200)}…`;
      else out[k] = v;
    }
    return out;
  }
  return body;
}
function pick(x: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const k of ["id", "name", "status", "code", "username", "role", "record_id", "session_id", "agent_id", "agent_name", "method", "kind", "mode", "type", "record_count"]) if (k in x) out[k] = x[k];
  return out;
}

function wire(s: Session, page: Page) {
  page.setDefaultTimeout(20_000);
  page.on("console", (m) => {
    if (m.type() === "error") s.consoleErrors.push({ at: new Date().toISOString(), url: page.url(), text: m.text().slice(0, 300) });
  });
  page.on("pageerror", (e) => s.pageErrors.push({ at: new Date().toISOString(), url: page.url(), text: String(e.message).slice(0, 300) }));
  page.on("requestfailed", (r) => {
    if (r.url().includes("/api/")) s.failed.push({ url: r.url().slice(0, 200), error: r.failure()?.errorText });
  });
}

export interface Api {
  get: (p: string, headers?: Record<string, string>) => Promise<ApiRes>;
  post: (p: string, data?: unknown, headers?: Record<string, string>) => Promise<ApiRes>;
  put: (p: string, data?: unknown) => Promise<ApiRes>;
  patch: (p: string, data?: unknown) => Promise<ApiRes>;
  delete: (p: string) => Promise<ApiRes>;
  raw: APIRequestContext;
}
export interface ApiRes {
  status: number;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  body: any;
}
export function apiOf(request: APIRequestContext): Api {
  const call = async (method: string, p: string, data?: unknown, headers?: Record<string, string>): Promise<ApiRes> => {
    const r = await request.fetch(BASE + p, { method, data, headers: { ...(headers ?? {}) }, timeout: 90_000, maxRedirects: 0 });
    let body: unknown = null;
    try {
      body = await r.json();
    } catch {
      body = (await r.text().catch(() => "")).slice(0, 300);
    }
    return { status: r.status(), body };
  };
  return {
    get: (p, h) => call("GET", p, undefined, h),
    post: (p, d, h) => call("POST", p, d, h),
    put: (p, d) => call("PUT", p, d),
    patch: (p, d) => call("PATCH", p, d),
    delete: (p) => call("DELETE", p),
    raw: request,
  };
}

/** A signed-in browser context (cookie set through the API client, never typed). */
export async function session(browser: Browser, role: "admin" | "member" | "anon", opts: { username?: string; password?: string } = {}): Promise<Session> {
  const ctx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1600, height: 1000 }, locale: "en-US" });
  const s: Session = { role, username: opts.username ?? null, ctx, page: null as unknown as Page, api: apiOf(ctx.request), hits: [], consoleErrors: [], pageErrors: [], failed: [] };
  ctx.on("response", async (res) => {
    const u = new URL(res.url());
    if (!u.pathname.startsWith("/api/")) return;
    let body: unknown = null;
    try {
      if ((res.headers()["content-type"] ?? "").includes("json")) body = summarize(await res.json());
    } catch {
      /* streamed / closed */
    }
    const req = res.request();
    let reqBody: unknown;
    if (req.method() !== "GET") {
      try {
        reqBody = req.postDataJSON();
      } catch {
        reqBody = undefined;
      }
    }
    s.hits.push({ at: new Date().toISOString(), method: req.method(), path: u.pathname + u.search, status: res.status(), ...(reqBody !== undefined ? { req: redact(reqBody) } : {}), body });
  });
  ctx.on("page", (p) => wire(s, p));
  s.page = await ctx.newPage();
  wire(s, s.page);
  if (role !== "anon") {
    const username = opts.username ?? (role === "admin" ? process.env.LAUNCHPAD_E2E_USERNAME ?? "admin" : MEMBER);
    const password = opts.password ?? (role === "admin" ? process.env.LAUNCHPAD_E2E_PASSWORD : memberPassword());
    if (!password) throw new Error(`password for ${role} not in the environment (LAUNCHPAD_E2E_PASSWORD)`);
    const r = await s.api.post("/api/auth/login", { username, password });
    if (r.status !== 200) throw new Error(`login ${username} → ${r.status} ${JSON.stringify(redact(r.body)).slice(0, 200)}`);
    s.username = username;
    const st = await s.api.get("/api/auth/status");
    log("signed in", username, { role: st.body?.role, user_id: st.body?.user_id });
  }
  return s;
}

/* ───────────── navigation / page probes ───────────── */
export async function goto(page: Page, url: string, settle = 800): Promise<void> {
  await page.goto(url.startsWith("http") ? url : BASE + url, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle", { timeout: 20_000 }).catch(() => {});
  await page.waitForTimeout(settle);
}
export async function shot(page: Page, name: string): Promise<string> {
  const leak = await page.evaluate(() => Array.from(document.querySelectorAll<HTMLInputElement>("input[type=password]")).some((i) => i.value)).catch(() => false);
  if (leak) throw new Error(`refusing screenshot ${name}: a password field holds text`);
  const file = `${name.replace(/[^a-zA-Z0-9._-]+/g, "_").slice(0, 150)}.png`;
  await page.screenshot({ path: path.join(SHOTS, file), fullPage: false }).catch(() => {});
  return file;
}
export async function bodyText(page: Page): Promise<string> {
  return (await page.locator("body").innerText().catch(() => "")).replace(/\s+/g, " ");
}
export function mark(s: Session) {
  return { hits: s.hits.length, consoleErrors: s.consoleErrors.length, pageErrors: s.pageErrors.length, failed: s.failed.length };
}
export function since(s: Session, m: ReturnType<typeof mark>) {
  return { hits: s.hits.slice(m.hits), consoleErrors: s.consoleErrors.slice(m.consoleErrors), pageErrors: s.pageErrors.slice(m.pageErrors), failed: s.failed.slice(m.failed) };
}

/** Pick an option of a V2 `Select` / `FilterSelect` (custom listbox) by label regex. */
export async function pickOption(page: Page, trigger: ReturnType<Page["locator"]>, label: RegExp | string): Promise<void> {
  await trigger.click();
  const pop = page.locator(".v2-pop").last();
  await pop.waitFor({ state: "visible", timeout: 5000 });
  const search = pop.locator(".v2-pop-search input");
  if (typeof label === "string" && (await search.count())) await search.fill(label);
  const opt = pop.locator("[role=option]").filter({ hasText: label }).first();
  await opt.click();
  await pop.waitFor({ state: "hidden", timeout: 5000 }).catch(() => {});
}
/** The option labels (and disabled flags) a V2 listbox offers; closes it again. */
export async function listOptions(page: Page, trigger: ReturnType<Page["locator"]>): Promise<{ label: string; disabled: boolean }[]> {
  await trigger.click();
  const pop = page.locator(".v2-pop").last();
  await pop.waitFor({ state: "visible", timeout: 5000 });
  const out = await pop.locator("[role=option]").evaluateAll((els) => els.map((e) => ({ label: (e.textContent ?? "").trim(), disabled: e.classList.contains("disabled") || e.getAttribute("aria-disabled") === "true" })));
  await page.keyboard.press("Escape");
  await pop.waitFor({ state: "hidden", timeout: 5000 }).catch(() => {});
  return out;
}

export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
export async function waitFor<T>(fn: () => Promise<T | null | false | undefined>, opts: { timeoutMs?: number; everyMs?: number; label?: string } = {}): Promise<T | null> {
  const t0 = Date.now();
  const timeoutMs = opts.timeoutMs ?? 60_000;
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() - t0 > timeoutMs) {
      log("waitFor timeout", opts.label ?? "");
      return null;
    }
    await sleep(opts.everyMs ?? 3000);
  }
}

/* ───────────── fixtures ───────────── */
export interface AgentRow {
  id: string;
  name: string;
  method: string;
  status: string;
  created_by_name?: string | null;
}
export async function listAgents(api: Api): Promise<AgentRow[]> {
  const r = await api.get("/api/agents");
  return (r.body?.agents ?? []) as AgentRow[];
}
export async function waitForAgent(api: Api, name: string, status: string, timeoutMs: number): Promise<AgentRow | null> {
  return waitFor(async () => (await listAgents(api)).find((a) => a.name === name && a.status === status) ?? null, { timeoutMs, everyMs: 15_000, label: `agent ${name} ${status}` });
}
/** Deletes every `prefix` agent whose deploy has SETTLED. A row still `deploying` is never
 *  deleted (N16-obs83: the deploy job has no status check and would leave real AWS orphans) —
 *  the sweep waits up to `settleMs` for it and otherwise reports it as left. */
export async function deleteAgentsWithPrefix(api: Api, prefix = PREFIX, settleMs = 20 * 60_000): Promise<string[]> {
  const gone: string[] = [];
  await waitFor(async () => !(await listAgents(api)).some((a) => a.name.startsWith(prefix) && a.status === "deploying"), { timeoutMs: settleMs, everyMs: 15_000, label: "x16n deploys settle" });
  for (const a of await listAgents(api)) {
    if (!a.name.startsWith(prefix)) continue;
    if (a.status === "deploying") {
      log("NOT deleting (still deploying, N16-obs83)", a.name);
      continue;
    }
    const r = await api.delete(`/api/agents/${encodeURIComponent(a.id)}`);
    log("delete agent", a.name, r.status, r.body?.failed ?? "");
    if (r.status < 300) gone.push(a.name);
  }
  return gone;
}
/** One console turn; returns the session id the server used. */
export async function chatOnce(api: Api, agentId: string, prompt: string, sessionId: string | null = null): Promise<{ status: number; session_id: string | null; text: string }> {
  const r = await api.raw.fetch(`${BASE}/api/chat/${encodeURIComponent(agentId)}`, { method: "POST", data: { prompt, session_id: sessionId }, timeout: 180_000 });
  const text = await r.text().catch(() => "");
  const m = text.match(/"session_id"\s*:\s*"([^"]+)"/);
  return { status: r.status(), session_id: m ? m[1] : sessionId, text: text.slice(0, 2000) };
}

export const GT_DATASET_ITEMS = [1, 2, 3].map((i) => ({
  scenario_id: `x16n-gt-${i}`,
  turns: [{ input: `What is ${i} plus ${i}? Answer with the number only.`, expected_response: String(i + i) }],
}));
export const SIM_DATASET_ITEMS = [1, 2, 3].map((i) => ({
  scenario_id: `x16n-sim-${i}`,
  input: `Hello, I need help with request ${i}.`,
  actor_profile: { context: `An employee asking question ${i} about office hours.`, goal: "Learn when the office opens." },
}));
export async function ensureDataset(api: Api, name: string, items: unknown[]): Promise<{ id: string; has_ground_truth: boolean; kind: string }> {
  const list = await api.get("/api/eval/datasets");
  const found = (list.body?.datasets ?? []).find((d: { name: string }) => d.name === name);
  if (found) return found;
  const r = await api.post("/api/eval/datasets", { name, description: "round-16 combination fixture", items });
  if (r.status >= 300) throw new Error(`dataset ${name} → ${r.status} ${JSON.stringify(r.body).slice(0, 200)}`);
  log("dataset created", name, r.body?.id, r.body?.kind);
  return r.body;
}
export async function deleteDatasetsWithPrefix(api: Api, prefix = PREFIX): Promise<string[]> {
  const list = await api.get("/api/eval/datasets");
  const gone: string[] = [];
  for (const d of list.body?.datasets ?? []) {
    if (!String(d.name).startsWith(prefix)) continue;
    const r = await api.delete(`/api/eval/datasets/${encodeURIComponent(d.id)}?force=true`);
    log("delete dataset", d.name, r.status);
    if (r.status < 300) gone.push(d.name);
  }
  return gone;
}
/** Stop (if running) then delete every x16n run / online config. */
export async function deleteTasksWithPrefix(api: Api, prefix = PREFIX): Promise<{ runs: number; online: number; left: string[] }> {
  const left: string[] = [];
  let runs = 0;
  let online = 0;
  const rl = await api.get("/api/eval/runs?limit=200");
  for (const r of rl.body?.runs ?? []) {
    if (!String(r.name ?? "").startsWith(prefix)) continue;
    if (!["failed", "stopped", "completed"].includes(r.status)) await api.post(`/api/eval/runs/${encodeURIComponent(r.id)}/stop`);
    let d = await api.delete(`/api/eval/runs/${encodeURIComponent(r.id)}`);
    if (d.status >= 300) {
      await sleep(3000);
      d = await api.delete(`/api/eval/runs/${encodeURIComponent(r.id)}`);
    }
    if (d.status < 300) runs += 1;
    else left.push(`run ${r.id} ${r.status} → ${d.status} ${d.body?.code ?? ""}`);
  }
  const ol = await api.get("/api/eval/online");
  for (const c of ol.body?.configs ?? []) {
    if (!String(c.description ?? c.name ?? "").startsWith(prefix)) continue;
    const d = await api.delete(`/api/eval/online/${encodeURIComponent(c.config_id)}`);
    if (d.status < 300) online += 1;
    else left.push(`online ${c.config_id} → ${d.status}`);
  }
  return { runs, online, left };
}

/** The member fixture: registered through the public form, approved + granted the default
 *  workspace by the admin. Returns its user id. Password only from the environment. */
/** The member fixture's password: `LAUNCHPAD_E2E_MEMBER_PASSWORD`, or — so that separate
 *  suite invocations agree without anything on disk — a digest of the admin password held in
 *  memory. Never logged, never written. */
export function memberPassword(): string {
  if (process.env.LAUNCHPAD_E2E_MEMBER_PASSWORD) return process.env.LAUNCHPAD_E2E_MEMBER_PASSWORD;
  const seed = process.env.LAUNCHPAD_E2E_PASSWORD;
  if (!seed) throw new Error("neither LAUNCHPAD_E2E_MEMBER_PASSWORD nor LAUNCHPAD_E2E_PASSWORD is set");
  return `X16n-${createHash("sha256").update(`${seed}:${MEMBER}`).digest("hex").slice(0, 20)}`;
}
export async function ensureMember(admin: Api): Promise<string> {
  const password = memberPassword();
  const find = async () => ((await admin.get(`/api/users?q=${MEMBER}&limit=50`)).body?.items ?? []).find((u: { username: string }) => u.username === MEMBER);
  let user = await find();
  if (!user) {
    const r = await admin.post("/api/auth/register", { username: MEMBER, email: `${MEMBER}@example.invalid`, password });
    if (r.status >= 300) throw new Error(`register ${MEMBER} → ${r.status} ${JSON.stringify(redact(r.body)).slice(0, 200)}`);
    user = await find();
  }
  if (!user) throw new Error("member registered but not listed");
  if (user.status !== "active") {
    const r = await admin.patch(`/api/users/${encodeURIComponent(user.id)}`, { status: "active", workspace_ids: ["default"] });
    if (r.status >= 300) throw new Error(`approve ${MEMBER} → ${r.status} ${JSON.stringify(redact(r.body)).slice(0, 200)}`);
  } else {
    await admin.patch(`/api/users/${encodeURIComponent(user.id)}`, { workspaces: ["default"] });
  }
  log("member ready", MEMBER, user.id);
  return user.id as string;
}
export async function deleteMember(admin: Api): Promise<boolean> {
  const user = ((await admin.get(`/api/users?q=${MEMBER}&limit=50`)).body?.items ?? []).find((u: { username: string }) => u.username === MEMBER);
  if (!user) return true;
  const r = await admin.delete(`/api/users/${encodeURIComponent(user.id)}`);
  log("delete member", r.status);
  return r.status < 300;
}

/** Memory resources created by the suites (`x16n_*` — CreateMemory names use underscores). */
export async function deleteMemoryResourcesWithPrefix(api: Api, prefix = "x16n"): Promise<string[]> {
  const gone: string[] = [];
  const rows = ((await api.get("/api/memory/resources")).body?.items ?? []) as { id: string | null; name: string | null; status: string | null; is_default: boolean }[];
  for (const r of rows) {
    if (!r.id || r.is_default || !(r.name ?? "").startsWith(prefix)) continue;
    await waitFor(async () => {
      const d = (await api.get(`/api/memory/resources/${encodeURIComponent(r.id ?? "")}`)).body;
      return d?.status && d.status !== "CREATING" ? d : null;
    }, { timeoutMs: 8 * 60_000, everyMs: 10_000, label: `memory ${r.name} settle` });
    const d = await api.delete(`/api/memory/resources/${encodeURIComponent(r.id)}`);
    log("delete memory resource", r.name, d.status);
    if (d.status < 300) gone.push(r.name ?? r.id);
  }
  return gone;
}
/** Custom evaluators created by the suites (`x16n_*`). */
export async function deleteEvaluatorsWithPrefix(api: Api, prefix = "x16n"): Promise<string[]> {
  const gone: string[] = [];
  const rows = ((await api.get("/api/eval/evaluators")).body?.evaluators ?? []) as { id: string; name?: string | null; source: string }[];
  for (const e of rows) {
    if (e.source !== "custom" || !(e.name ?? e.id).startsWith(prefix)) continue;
    const d = await api.delete(`/api/eval/evaluators/${encodeURIComponent(e.id)}?force=true`);
    log("delete evaluator", e.name ?? e.id, d.status);
    if (d.status < 300) gone.push(e.name ?? e.id);
  }
  return gone;
}

export async function getVisibility(admin: Api): Promise<string> {
  return (await admin.get("/api/workspaces/default/settings/visibility")).body?.resource_visibility;
}
export async function setVisibility(admin: Api, value: "shared" | "isolated"): Promise<number> {
  const r = await admin.put("/api/workspaces/default/settings/visibility", { resource_visibility: value });
  log("visibility →", value, r.status);
  return r.status;
}

/**
 * Page pairwise (round 16, r16-n step 2): six high-density list pages × their controls
 * (page-factors.ts) × role × visibility mode. After each all-pairs row the suite asserts:
 *   rows shown == the API answer for the same filter (through the SAME role's cookie),
 *   paging neither drops nor repeats keys and the total label matches,
 *   the promised URL params are present and a reload reproduces the state,
 *   selection survives paging where the UX promises it (traces, scorecard picks),
 *   no page error / unexpected console error.
 * Fixtures: the x16n-member account, x16n registry records, a member-created dataset; the
 * w1/w2 agents, runs and datasets. The admin toggles shared/isolated on the default workspace
 * and the original value is restored in afterAll (and by w9-cleanup).
 */
import { expect, test, type Locator, type Page } from "@playwright/test";

import * as L from "./lib";
import { PAGE_CONSTRAINTS, PAGE_FACTORS, type PageKey, PAGES, pagePath, promisedParams } from "./page-factors";
import { allPairs, type Combo, comboLabel } from "./pairwise";

const SEED = Number(process.env.X16N_SEED ?? 16);
const ONLY = (process.env.X16N_PAGES ?? "").split(",").filter(Boolean) as PageKey[];
const FX_AGENT = process.env.X16N_TASK_AGENT ?? "x16n-fx-harness";
const SUITE = "page-pairwise";

// tests run in file order (workers: 1, fullyParallel: false); NOT serial mode — a soft failure in one test must not skip the rest

let admin: L.Session;
let member: L.Session;
let originalVis = "shared";
let currentVis = "shared";
let agentKey = "";
const notes: string[] = [];

test.beforeAll(async ({ browser }) => {
  admin = await L.session(browser, "admin");
  originalVis = await L.getVisibility(admin.api);
  currentVis = originalVis;
  await L.ensureMember(admin.api);
  member = await L.session(browser, "member");
  // registry fixtures (shared reads): two MCP records
  for (const n of [1, 2]) {
    const name = `x16n-mcp-${n}`;
    const have = ((await admin.api.get("/api/registry/records")).body?.records ?? []).some((r: { name: string }) => r.name === name);
    if (have) continue;
    const r = await admin.api.post("/api/registry/records", { type: "MCP", name, description: `round-16 combination fixture ${n}`, url: `https://example.invalid/x16n/mcp-${n}` });
    L.log("registry fixture", name, r.status, r.body?.code ?? "");
    if (r.status >= 300) notes.push(`registry fixture ${name}: ${r.status} ${r.body?.code ?? ""}`);
  }
  // a member-created dataset: visible to the admin always, to the member always; the point of
  // the isolation mode is what the member does NOT see of OTHER members — the admin's rows stay
  // visible, so this fixture mainly exercises the member's own rows under both modes
  await L.ensureDataset(member.api, "x16n-m-ds", L.GT_DATASET_ITEMS).catch((e) => notes.push(`member dataset: ${String(e).slice(0, 120)}`));
  const sc = (await admin.api.get("/api/eval/scorecard")).body ?? {};
  agentKey = (sc.agents ?? []).find((a: { name: string }) => a.name === FX_AGENT)?.key ?? (sc.agents ?? [])[0]?.key ?? "";
  const plan: Record<string, unknown> = {};
  for (const p of PAGES) {
    const r = allPairs(PAGE_FACTORS[p], { seed: SEED, constraint: PAGE_CONSTRAINTS[p] });
    plan[p] = { combos: r.combos.length, pairs: r.pairCount, product: r.fullProductSize };
  }
  L.writeJson("p1-plan.json", { seed: SEED, agentKey, notes, pages: plan });
});

test.afterAll(async () => {
  if (currentVis !== originalVis) await L.setVisibility(admin.api, originalVis as "shared" | "isolated");
  await admin.ctx.close();
  await member.ctx.close();
});

/* ───────────── generic helpers ───────────── */
const filterNth = (page: Page, i: number): Locator => page.locator(".v2-toolbar .v2-filter").nth(i);
async function pickNth(page: Page, trigger: Locator, index: number): Promise<string> {
  await trigger.click();
  const pop = page.locator(".v2-pop").last();
  await pop.waitFor({ state: "visible", timeout: 5000 });
  const opt = pop.locator("[role=option]").nth(index);
  const label = (await opt.innerText().catch(() => "")).trim();
  await opt.click();
  await pop.waitFor({ state: "hidden", timeout: 5000 }).catch(() => {});
  return label;
}
async function pickLabel(page: Page, trigger: Locator, re: RegExp): Promise<boolean> {
  await trigger.click();
  const pop = page.locator(".v2-pop").last();
  await pop.waitFor({ state: "visible", timeout: 5000 });
  const opt = pop.locator("[role=option]").filter({ hasText: re }).first();
  const ok = (await opt.count()) > 0;
  if (ok) await opt.click();
  else await page.keyboard.press("Escape");
  await pop.waitFor({ state: "hidden", timeout: 5000 }).catch(() => {});
  return ok;
}
async function search(page: Page, q: string, testId?: string) {
  const input = testId ? page.getByTestId(testId) : page.locator(".v2-toolbar .v2-search input").first();
  await input.fill(q);
  await page.waitForTimeout(400);
}
/** read the table keys of every page through the Pager; returns per-page key lists */
async function walkPages(page: Page, read: () => Promise<string[]>): Promise<{ pages: string[][]; total: number | null; label: string }> {
  const pages: string[][] = [];
  const pager = page.locator(".v2-pager").first();
  const hasPager = (await pager.count()) > 0;
  let label = hasPager ? ((await pager.locator("span").nth(1).innerText().catch(() => "")) ?? "") : "";
  const totalText = hasPager ? await pager.locator("span").first().innerText().catch(() => "") : "";
  const total = totalText ? Number((totalText.match(/\d+/) ?? [NaN])[0]) : null;
  // start from page 1
  if (hasPager) {
    const prev = pager.locator("button").nth(0);
    for (let i = 0; i < 50 && !(await prev.isDisabled()); i += 1) await prev.click();
  }
  for (let i = 0; i < 60; i += 1) {
    await page.waitForTimeout(150);
    pages.push(await read());
    if (!hasPager) break;
    const next = pager.locator("button").nth(1);
    if (await next.isDisabled()) break;
    await next.click();
  }
  if (hasPager) label = (await pager.locator("span").nth(1).innerText().catch(() => "")) ?? "";
  return { pages, total, label };
}
const q3 = (needle: string, ...fields: (string | null | undefined)[]) => !needle || fields.map((f) => f ?? "").join(" ").toLowerCase().includes(needle.toLowerCase());
/** rows of a V2 table, the empty-state row excluded */
const ROWS = "tbody tr:not(:has(.v2-table-empty))";
const testIdKeys = (page: Page, prefix: string) => page.locator(`[data-testid^="${prefix}"]`).evaluateAll((els, p) => els.map((e) => (e.getAttribute("data-testid") ?? "").slice((p as string).length)), prefix);

/* ───────────── per-page models ───────────── */
interface Outcome {
  expected: string[];
  /** DOM keys per pager page (page 1 first) */
  seen: string[][];
  total: number | null;
  /** extra findings */
  gaps: string[];
  notes: string[];
  screenshots: string[];
}

function runStatus(s: string): string {
  if (s === "queued") return "queued";
  if (["invoking", "waiting", "evaluating"].includes(s)) return "running";
  return s;
}
function runSource(r: { dataset_name?: string | null; dataset_id?: string | null; session_ids?: string[] }): string {
  const name = r.dataset_name ?? "";
  if (name.startsWith("window:")) return "window";
  if (name.startsWith("cloud:")) return "cloud";
  if (name.startsWith("logs:")) return "logs";
  if (!r.dataset_id && (r.session_ids ?? []).length > 0) return "sessions";
  return "dataset";
}
function onlineStatus(c: { status?: string; failure_reason?: string | null; execution_status?: string }): string {
  const st = (c.status ?? "").toUpperCase();
  if (c.failure_reason || st.includes("FAIL")) return "failed";
  if (st === "CREATING" || st === "UPDATING") return "pending";
  return (c.execution_status ?? "").toUpperCase() === "ENABLED" ? "running" : "paused";
}

const TASK_STATUSES = ["queued", "running", "completed", "failed", "stopped", "paused"];
const TASK_SOURCES = ["window", "sessions", "logs", "dataset", "cloud", "live"];
const REG_TYPES = ["A2A", "MCP", "AGENT_SKILLS"];
const REG_STATUSES = ["DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED", "DEPRECATED"];
const RANGES = ["1h", "6h", "24h", "7d"];
const USER_STATES = ["pending", "active", "expired", "disabled"];

async function driveTasks(s: L.Session, c: Combo, out: Outcome) {
  const page = s.page;
  if (c.tab === "all") {
    if (c.status !== "all") await pickNth(page, filterNth(page, 0), 1 + TASK_STATUSES.indexOf(c.status));
    if (c.source !== "all") await pickNth(page, filterNth(page, 1), 1 + TASK_SOURCES.indexOf(c.source));
    if (c.mode !== "all") await pickNth(page, filterNth(page, 2), 1 + ["evaluators", "insights"].indexOf(c.mode));
    if (c.agent === "fixture") {
      const ok = await pickLabel(page, filterNth(page, 3), new RegExp(`^${FX_AGENT}$`));
      if (!ok) out.notes.push("fixture agent not offered in the agent filter");
    }
    if (c.q) await search(page, c.q);
    const runs = (await s.api.get("/api/eval/runs?limit=200")).body?.runs ?? [];
    const online = ((await s.api.get("/api/eval/online")).body?.configs ?? []).filter((x: { owner: string; mode: string }) => x.owner === "agent" && x.mode === "scores");
    type T = { id: string; name: string; description: string; agentName: string; mode: string; source: string; status: string };
    const tasks: T[] = [
      ...runs.map((r: Record<string, unknown>) => {
        const agentName = (r.agent_name as string) ?? "";
        const src = runSource(r as never);
        const detail = src === "window" ? String(r.dataset_name).slice(7) : src === "cloud" ? String(r.dataset_name).slice(6) : src === "logs" || src === "sessions" ? String(((r.session_ids as string[]) ?? []).length) : String(r.dataset_name ?? "");
        return { id: String(r.id), name: (r.name as string) || `${agentName || r.id} · ${detail || r.id}`, description: (r.description as string) ?? "", agentName, mode: r.mode === "insights" ? "insights" : "evaluators", source: src, status: runStatus(String(r.status)) };
      }),
      ...online.map((o: Record<string, unknown>) => ({ id: String(o.config_id), name: String((o.description as string)?.trim() || o.name || o.config_id), description: (o.description as string) ?? "", agentName: String(o.agent_name ?? (o.matched_agent as { name?: string })?.name ?? "—"), mode: Array.isArray(o.insights) && (o.insights as string[]).length && !(o.evaluators as string[])?.length ? "insights" : "evaluators", source: "live", status: onlineStatus(o as never) })),
    ];
    out.expected = tasks
      .filter((t) => (c.status === "all" || t.status === c.status) && (c.source === "all" || t.source === c.source) && (c.mode === "all" || t.mode === c.mode) && (c.agent === "all" || t.agentName === FX_AGENT) && q3(c.q, t.name, t.id, t.description))
      .map((t) => t.id);
    const walked = await walkPages(page, async () => page.locator(`[data-testid="v2-tasks-table"] ${ROWS} .sub.mono`).evaluateAll((els) => els.map((e) => (e.textContent ?? "").replace(/^ID:\s*/, "").trim())));
    out.seen = walked.pages;
    out.total = walked.total;
  } else {
    if (c.q) await search(page, c.q, "v2-scorecard-search");
    const sc = (await s.api.get("/api/eval/scorecard")).body ?? {};
    out.expected = ((sc.agents ?? []) as { key: string; name: string }[]).filter((a) => q3(c.q, a.name, a.key)).map((a) => a.key);
    const walked = await walkPages(page, () => testIdKeys(page, "v2-scorecard-open-"));
    out.seen = walked.pages;
    out.total = walked.total;
    out.notes.push("agents tab: search matches name/key/main benchmark name (benchmark part not mirrored)");
  }
}

async function driveData(s: L.Session, c: Combo, out: Outcome) {
  const page = s.page;
  if (c.tab === "traces") {
    const traces = ((await s.api.get(`/api/observability/traces?range=${c.range}`)).body?.traces ?? []) as { trace_id: string; session_id: string | null; agent: string; root_operation: string; status: string }[];
    const agents = [...new Set(traces.map((r) => r.agent).filter(Boolean))].sort();
    if (c.agent === "fixture") {
      const ok = await pickLabel(page, filterNth(page, 1), new RegExp(`^${FX_AGENT}$`));
      if (!ok) out.notes.push(`fixture agent not in the agent filter (${agents.length} agents)`);
    }
    if (c.status !== "all") await pickNth(page, filterNth(page, 2), 1 + ["ok", "error"].indexOf(c.status));
    if (c.q) await search(page, c.q);
    // the table shows the first 12 characters of the trace id
    out.expected = traces.filter((r) => (c.agent === "all" || r.agent === FX_AGENT) && (c.status === "all" || r.status === c.status) && q3(c.q, r.trace_id, r.session_id, r.agent, r.root_operation)).map((r) => r.trace_id.slice(0, 12));
    // selection: survives paging, drives the add-to-dataset count
    const boxes = page.locator('[data-testid="v2-traces-table"] tbody input[type=checkbox]');
    const n = await boxes.count();
    if (c.select === "one" && n) await boxes.first().check();
    if (c.select === "pageAll" && n) await page.locator('[data-testid="v2-traces-table"] thead input[type=checkbox]').first().check();
    const addBtn = page.getByTestId("v2-traces-add");
    const addText = await addBtn.innerText().catch(() => "");
    const addDisabled = await addBtn.isDisabled().catch(() => true);
    const wantSelected = c.select === "one" ? Math.min(1, n) : c.select === "pageAll" ? n : 0;
    if (wantSelected > 0 && addDisabled) out.gaps.push("rows selected but add-to-dataset disabled");
    if (wantSelected === 0 && !addDisabled) out.gaps.push("nothing selected but add-to-dataset enabled");
    out.notes.push(`select=${c.select} rows=${n} add="${addText.trim()}"`);
    // the first cell is the selection box; the trace id is the row's first mono span
    const walked = await walkPages(page, () => page.locator(`[data-testid="v2-traces-table"] ${ROWS}`).evaluateAll((trs) => trs.map((tr) => (tr.querySelector(".mono")?.textContent ?? tr.textContent ?? "").trim().replace(/…$/, "").split(/\s+/)[0])));
    out.seen = walked.pages;
    out.total = walked.total;
    if (wantSelected > 0 && walked.pages.length > 1) {
      // back on the last page: the selection count must still be what we picked
      const after = await addBtn.innerText().catch(() => "");
      if (after.trim() !== addText.trim()) out.gaps.push(`selection changed across paging: "${addText.trim()}" → "${after.trim()}"`);
    }
  } else {
    if (c.kind !== "all") await pickNth(page, filterNth(page, 0), 1 + ["predefined", "legacy", "simulated"].indexOf(c.kind));
    if (c.origin !== "all") await pickNth(page, filterNth(page, 1), 1 + ["trace", "manual"].indexOf(c.origin));
    if (c.q) await search(page, c.q);
    const ds = ((await s.api.get("/api/eval/datasets")).body?.datasets ?? []) as { id: string; name: string; description: string; kind: string; items: { metadata?: { source?: string } }[] }[];
    const origin = (d: (typeof ds)[number]) => (d.items.some((i) => i.metadata?.source === "trace") ? "trace" : "manual");
    out.expected = ds.filter((d) => (c.kind === "all" || d.kind === c.kind) && (c.origin === "all" || origin(d) === c.origin) && q3(c.q, d.name, d.description)).map((d) => d.name);
    const walked = await walkPages(page, () => page.locator(`[data-testid="v2-datasets-table"] ${ROWS} td:first-child`).evaluateAll((tds) => tds.map((td) => (td.querySelector("button, a, .ellipsis, span")?.textContent ?? td.textContent ?? "").trim())));
    out.seen = walked.pages.map((p) => p.map((k) => out.expected.find((e) => k.startsWith(e) || k === e) ?? k));
    out.total = walked.total;
  }
}

async function driveScorecard(s: L.Session, c: Combo, out: Outcome) {
  const page = s.page;
  const sc = (await s.api.get("/api/eval/scorecard")).body ?? {};
  type Run = { id: string; agent_key: string; status: string; benchmark: { key: string }; scores: { score: number | null }[] };
  const runs = ((sc.runs ?? []) as Run[]).filter((r) => r.agent_key === agentKey);
  const scored = (r: Run) => r.status === "completed" && r.scores.some((x) => typeof x.score === "number");
  // benchmarksOf (frontend/src/v2/scorecard.ts:102) mirrored: by key, sorted by scored count / recency
  const by = new Map<string, { key: string; runs: Run[]; scored: number; lastScored: number; lastAny: number }>();
  runs.forEach((r, i) => {
    const b = by.get(r.benchmark.key) ?? { key: r.benchmark.key, runs: [], scored: 0, lastScored: -1, lastAny: -1 };
    b.runs.push(r);
    b.lastAny = i;
    if (scored(r)) {
      b.scored += 1;
      b.lastScored = i;
    }
    by.set(r.benchmark.key, b);
  });
  const benches = [...by.values()].sort((a, b) => b.scored - a.scored || b.lastScored - a.lastScored || b.lastAny - a.lastAny);
  const tabs = page.locator('[data-testid^="v2-scorecard-bench-"]');
  const nTabs = await tabs.count();
  const idx = c.bench === "second" ? 1 : 0;
  if (idx < nTabs) await tabs.nth(idx).click();
  else out.notes.push(`bench tab ${c.bench} absent (${nTabs} tabs)`);
  const bench = benches[Math.min(idx, benches.length - 1)];
  const toggle = page.locator('[data-testid="v2-scorecard-runs-card"] .v2-toolbar input[type=checkbox], [data-testid="v2-scorecard-runs-card"] label.v2-check input[type=checkbox]').first();
  if (await toggle.count()) {
    if ((await toggle.isChecked()) !== (c.onlyBench === "on")) await toggle.click();
  } else out.notes.push("only-benchmark toggle not found");
  const list = [...(c.onlyBench === "on" && bench ? bench.runs : runs)].reverse();
  out.expected = list.map((r) => r.id);
  // picks: scored runs only; the compare button needs exactly two
  const pickBoxes = page.locator('input[data-testid^="v2-scorecard-pick-"]:not([disabled])');
  const nPick = await pickBoxes.count();
  const want = c.pick === "two" ? 2 : c.pick === "one" ? 1 : 0;
  for (let i = 0; i < Math.min(want, nPick); i += 1) await pickBoxes.nth(i).check();
  const compare = page.getByTestId("v2-scorecard-pick-compare");
  if ((await compare.count()) > 0) {
    const dis = await compare.isDisabled();
    if (want === 2 && nPick >= 2 && dis) out.gaps.push("two runs picked but compare disabled");
    if (want < 2 && !dis) out.gaps.push(`${want} picked but compare enabled`);
  } else if (want > 0 && nPick > 0) out.notes.push("compare button not rendered with picks");
  if (want > 0 && nPick === 0) out.notes.push("no scored run to pick");
  const walked = await walkPages(page, () => testIdKeys(page, "v2-scorecard-run-open-"));
  out.seen = walked.pages;
  out.total = walked.total;
  if (want > 0 && nPick > 0 && walked.pages.length > 1) {
    const stillChecked = await page.locator('input[data-testid^="v2-scorecard-pick-"]:checked').count();
    const ab = await page.locator(".v2-compare-ab").count();
    if (stillChecked + ab === 0) out.gaps.push("picks lost after paging");
  }
}

async function driveAgents(s: L.Session, c: Combo, out: Outcome) {
  const page = s.page;
  const agents = ((await s.api.get("/api/agents")).body?.agents ?? []) as { id: string; name: string; method: string; status: string }[];
  const methods = [...new Set(agents.map((a) => a.method))].sort();
  const statuses = [...new Set(agents.map((a) => a.status))].sort();
  if (c.method !== "all") {
    if (methods.includes(c.method)) await pickNth(page, filterNth(page, 0), 1 + methods.indexOf(c.method));
    else out.notes.push(`method ${c.method} not offered (none exists)`);
  }
  if (c.status !== "all") {
    if (statuses.includes(c.status)) await pickNth(page, filterNth(page, 1), 1 + statuses.indexOf(c.status));
    else out.notes.push(`status ${c.status} not offered (none exists)`);
  }
  if (c.q) await search(page, c.q, "v2-agents-search");
  const mOk = c.method === "all" || methods.includes(c.method);
  const sOk = c.status === "all" || statuses.includes(c.status);
  out.expected = agents.filter((a) => (!mOk || c.method === "all" || a.method === c.method) && (!sOk || c.status === "all" || a.status === c.status) && q3(c.q, a.name, a.id)).map((a) => a.name);
  const walked = await walkPages(page, () => page.locator('[data-testid="v2-agents-table"] tbody tr td:first-child button').evaluateAll((bs) => bs.map((b) => (b.textContent ?? "").trim())));
  out.seen = walked.pages;
  out.total = walked.total;
  // the KPI strip must agree with the unfiltered list
  const kpi = await page.getByTestId("v2-agents-kpi-total").innerText().catch(() => "");
  const kpiN = Number((kpi.match(/\d+/) ?? [NaN])[0]);
  if (!Number.isNaN(kpiN) && kpiN !== agents.length) out.gaps.push(`KPI total ${kpiN} ≠ API ${agents.length}`);
}

async function driveRegistry(s: L.Session, c: Combo, out: Outcome) {
  const page = s.page;
  if (c.tab === "records") {
    if (c.type !== "all") await pickNth(page, page.getByTestId("v2-registry-type"), 1 + REG_TYPES.indexOf(c.type));
    if (c.status !== "all") await pickNth(page, filterNth(page, 1), 1 + REG_STATUSES.indexOf(c.status));
    if (c.q) await search(page, c.q, "v2-registry-search");
    const recs = ((await s.api.get("/api/registry/records")).body?.records ?? []) as { record_id: string; name: string; type: string; status: string; description?: string }[];
    out.expected = recs.filter((r) => (c.type === "all" || r.type === c.type) && (c.status === "all" || r.status === c.status) && q3(c.q, r.name, r.record_id, r.description)).map((r) => r.record_id);
    const walked = await walkPages(page, () => testIdKeys(page, "v2-registry-open-"));
    out.seen = walked.pages;
    out.total = walked.total;
  } else {
    if (c.type !== "all") await pickNth(page, filterNth(page, 0), 1 + REG_TYPES.indexOf(c.type));
    if (c.q) await search(page, c.q);
    // the consumer view's list call is whatever the page fetched: compare to its answer
    const hit = [...s.hits].reverse().find((h) => h.method === "GET" && /\/api\/registry\/(discover|consumer|search)/.test(h.path));
    const walked = await walkPages(page, () => page.locator(`[data-testid="v2-registry-consumer-table"] ${ROWS} td:first-child`).evaluateAll((tds) => tds.map((td) => (td.textContent ?? "").trim())));
    out.seen = walked.pages;
    out.total = walked.total;
    const apiLen = (hit?.body as { records?: { _len: number } } | undefined)?.records?._len;
    out.expected = walked.pages.flat(); // no independent oracle; the count check below uses apiLen
    if (c.type === "all" && !c.q && apiLen !== undefined && apiLen !== walked.pages.flat().length) out.gaps.push(`discoverable rows ${walked.pages.flat().length} ≠ API ${apiLen} (${hit?.path})`);
    out.notes.push(`discoverable: API ${hit?.path ?? "?"} len=${apiLen ?? "?"}`);
  }
}

async function driveUsers(s: L.Session, c: Combo, out: Outcome) {
  const page = s.page;
  if (c.role !== "admin") {
    // a member must see a readable refusal, never a broken page
    const text = await L.bodyText(page);
    const table = await page.getByTestId("v2-users-table").count();
    if (table) out.gaps.push("member sees the users table");
    if (!/admin|permission|forbidden|not allowed|403|管理员|权限/i.test(text)) out.gaps.push("no readable refusal for a member");
    out.notes.push(`member view: ${text.slice(0, 120)}`);
    out.expected = [];
    out.seen = [[]];
    return;
  }
  if (c.status !== "all") await pickNth(page, page.getByTestId("v2-users-status"), 1 + USER_STATES.indexOf(c.status));
  if (c.q) await search(page, c.q, "v2-users-search");
  await page.waitForTimeout(600);
  const qs = new URLSearchParams();
  if (c.q) qs.set("q", c.q);
  if (c.status !== "all") qs.set("status", c.status);
  qs.set("limit", "25");
  const total = (await s.api.get(`/api/users?${qs.toString()}&offset=0`)).body?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / 25));
  const expectedPages: string[][] = [];
  for (let p = 0; p < pages; p += 1) {
    const body = (await s.api.get(`/api/users?${qs.toString()}&offset=${p * 25}`)).body ?? {};
    expectedPages.push(((body.items ?? []) as { username: string }[]).map((u) => u.username));
  }
  out.expected = expectedPages.flat();
  const walked = await walkPages(page, () => testIdKeys(page, "v2-users-open-"));
  out.seen = walked.pages;
  out.total = walked.total ?? total;
  // server-paged: order must match page by page
  if (JSON.stringify(walked.pages) !== JSON.stringify(expectedPages) && total > 0) out.gaps.push(`page order differs: ui ${JSON.stringify(walked.pages).slice(0, 200)} api ${JSON.stringify(expectedPages).slice(0, 200)}`);
}

const DRIVERS: Record<PageKey, (s: L.Session, c: Combo, out: Outcome) => Promise<void>> = { tasks: driveTasks, data: driveData, scorecard: driveScorecard, agents: driveAgents, registry: driveRegistry, users: driveUsers };

/* ───────────── the row ───────────── */
async function runRow(pageKey: PageKey, idx: string, c: Combo) {
  const id = `${pageKey}-${idx}`;
  if (c.vis && c.vis !== currentVis) {
    await L.setVisibility(admin.api, c.vis as "shared" | "isolated");
    currentVis = c.vis;
  }
  const s = c.role === "member" ? member : admin;
  const m = L.mark(s);
  const out: Outcome = { expected: [], seen: [], total: null, gaps: [], notes: [], screenshots: [] };
  const url = pagePath(pageKey, c, { agentKey });
  try {
    await L.goto(s.page, url, 600);
    await DRIVERS[pageKey](s, c, out);
    out.screenshots.push(await L.shot(s.page, `P-${id}`));
    // consistency: set equality, no duplicates across pages, totals
    const flat = out.seen.flat();
    const dup = flat.filter((k, i) => flat.indexOf(k) !== i);
    if (dup.length) out.gaps.push(`duplicate keys across pages: ${dup.slice(0, 5).join(", ")}`);
    const exp = new Set(out.expected);
    const got = new Set(flat);
    const missing = [...exp].filter((k) => !got.has(k));
    const extra = [...got].filter((k) => !exp.has(k));
    if (pageKey !== "registry" || c.tab === "records") {
      if (missing.length || extra.length) out.gaps.push(`rows ≠ API: missing ${missing.length} [${missing.slice(0, 3).join(", ")}] extra ${extra.length} [${extra.slice(0, 3).join(", ")}] (ui ${flat.length} / api ${exp.size})`);
    }
    if (out.total !== null && !Number.isNaN(out.total) && out.total !== exp.size && (pageKey !== "registry" || c.tab === "records")) out.gaps.push(`total label ${out.total} ≠ API ${exp.size}`);
    if (c.page === "last" && out.seen.length > 1 && out.seen[out.seen.length - 1].length === 0) out.gaps.push("last page is empty");
    // URL promises + reload reproduces them
    const promised = promisedParams(pageKey, c);
    const u1 = new URL(s.page.url());
    for (const [k, v] of Object.entries(promised)) {
      const have = u1.searchParams.get(k);
      if (v === null ? have !== null : have !== v) out.gaps.push(`url ${k}=${have} expected ${v}`);
    }
    await s.page.reload({ waitUntil: "domcontentloaded" });
    await s.page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});
    await s.page.waitForTimeout(500);
    const u2 = new URL(s.page.url());
    for (const [k, v] of Object.entries(promised)) {
      const have = u2.searchParams.get(k);
      if (v === null ? have !== null : have !== v) out.gaps.push(`after reload url ${k}=${have} expected ${v}`);
    }
    const text = await L.bodyText(s.page);
    if (text.length < 40) out.gaps.push("blank page after reload");
  } catch (err) {
    out.gaps.push(`drive error: ${String(err).slice(0, 200)}`);
    out.screenshots.push(await L.shot(s.page, `P-${id}-error`));
  }
  const d = L.since(s, m);
  const consoleErrors = d.consoleErrors.filter((e) => !/Failed to load resource/.test(e.text));
  if (d.pageErrors.length) out.gaps.push(`pageErrors: ${d.pageErrors.map((e) => e.text).join(" | ").slice(0, 200)}`);
  if (consoleErrors.length) out.gaps.push(`consoleErrors: ${consoleErrors.map((e) => e.text).join(" | ").slice(0, 200)}`);
  const verdict: L.Result["verdict"] = out.gaps.length ? "FAIL" : "PASS";
  const cls: L.Cls | undefined = !out.gaps.length ? undefined : d.pageErrors.length ? "BROKEN" : out.gaps.some((g) => g.startsWith("rows ≠") || g.startsWith("total") || g.startsWith("duplicate") || g.startsWith("page order") || g.startsWith("KPI") || g.startsWith("discoverable")) ? "INCONSISTENT" : "UX";
  L.record({ suite: SUITE, id, verdict, cls, role: c.role, combo: c, expected: `${out.expected.length} rows; url ${JSON.stringify(promisedParams(pageKey, c))}`, observed: [`${out.seen.flat().length} rows over ${out.seen.length} page(s), total=${out.total}`, ...out.gaps, ...out.notes].join(" · "), screenshots: out.screenshots, repro: { seed: SEED, url, combo: comboLabel(c) } });
  expect.soft(verdict, `${id} ${comboLabel(c)} :: ${out.gaps.join(" | ")}`).toBe("PASS");
}

for (const pageKey of PAGES) {
  test(`page pairwise: ${pageKey}`, async () => {
    if (ONLY.length && !ONLY.includes(pageKey)) test.skip();
    const r = allPairs(PAGE_FACTORS[pageKey], { seed: SEED, constraint: PAGE_CONSTRAINTS[pageKey] });
    L.log(`p1 ${pageKey}`, { combos: r.combos.length, pairs: r.pairCount, product: r.fullProductSize });
    for (const [i, c] of r.combos.entries()) await runRow(pageKey, String(i).padStart(2, "0"), c);
  });
}

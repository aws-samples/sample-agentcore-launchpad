#!/usr/bin/env node
// Click-through effect crawler for the V2 console (round 16, r16-m).
//
// Drives a LIVE console: for every row of frontend/src/v2/__inventory__/controls.json ×
// role (admin, member) it navigates to the control's page, asserts the control is present
// (or absent exactly when the inventory's `roles` says so), CLICKS it and asserts the
// inventory's expected effect within 5 s — an `/api` request matching the route pattern,
// a URL change, a dialog with the expected testId, or a DOM delta — recording network,
// console errors and page errors. Destructive controls are clicked only up to their
// Confirm dialog, which is then CANCELLED (Escape); a destructive control whose effect is a
// direct mutation is skipped unless the row belongs to an `x16m-` fixture.
//
// Every miss is classified: DEAD (no effect), BROKEN (effect fired but 4xx/5xx or console
// error), MISLABELED (effect differs from the inventory), DISABLED-NO-HINT (disabled with no
// reason tooltip — the r14-b/r15-d contract), ROLE-LEAK (visible to a role the inventory
// says may not see it), FLAKY (passed on retry), NOT-FOUND (not reachable in the crawled
// page state), ERROR (crawler exception).
//
// NOT part of `make verify` (live). Env:
//   LAUNCHPAD_E2E_BASE_URL   https://<host>:8443 (required)
//   LAUNCHPAD_E2E_USERNAME   admin account (default "admin")
//   LAUNCHPAD_E2E_PASSWORD   its password — fetch in-process from SSM; never written anywhere
//   X16M_MEMBER_PASSWORD     optional fixed member password (otherwise random, memory only)
//   PLAYWRIGHT_MODULE        path of the `playwright` package (default: tests/e2e/node_modules)
//   CRAWL_OUT_DIR            where crawl-<date>.json / .jsonl go (default: this directory/out)
//   CRAWL_SHOTS              screenshot dir for non-green cells (default: CRAWL_OUT_DIR/shots)
// Flags:
//   --fixtures=create|keep|delete-only   create x16m- fixtures first (default create); delete-only = teardown
//   --deploy                 also deploy one small harness agent (x16m-agent); otherwise reuse
//                            the first active agent for the read-only agent controls
//   --roles=admin,member     subset of roles (default both)
//   --pages=/v2/agents,...   subset of pages (default all)
//   --kinds=toggle,tab       subset of control kinds (default all)
//   --limit=N                stop after N controls (smoke)
//   --resume                 skip controls already in the JSONL
//   --redo=NOT-FOUND[,…]     re-crawl the controls whose LAST recorded status is listed (plus every control
//                            not crawled yet), trying up to
//                            4 "openers" on the same page first (a control whose effect is a dialog or a
//                            ?view= switch, preferring the same file) so controls inside dialogs / sub-forms
//                            become reachable; the aggregate keeps the last record per role × control
//   --date=YYYYMMDD          output stamp (default today)
import { createRequire } from "node:module";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "../../../..");
const requireFrom = createRequire(import.meta.url);
const PW_PATH = process.env.PLAYWRIGHT_MODULE ?? (fs.existsSync(path.join(REPO, "tests/e2e/node_modules/playwright")) ? path.join(REPO, "tests/e2e/node_modules/playwright") : "playwright");
const { chromium } = requireFrom(PW_PATH);

const args = Object.fromEntries(process.argv.slice(2).map((a) => {
  const m = a.match(/^--([^=]+)(?:=(.*))?$/);
  return m ? [m[1], m[2] ?? true] : [a, true];
}));
const BASE = process.env.LAUNCHPAD_E2E_BASE_URL;
const ADMIN_USER = process.env.LAUNCHPAD_E2E_USERNAME ?? "admin";
const ADMIN_PASS = process.env.LAUNCHPAD_E2E_PASSWORD;
if (!BASE || !ADMIN_PASS) {
  console.error("LAUNCHPAD_E2E_BASE_URL and LAUNCHPAD_E2E_PASSWORD are required (fetch the password in-process from SSM)");
  process.exit(2);
}
const DATE = args.date ?? new Date().toISOString().slice(0, 10).replace(/-/g, "");
const OUT_DIR = process.env.CRAWL_OUT_DIR ?? path.join(HERE, "out");
const SHOTS = process.env.CRAWL_SHOTS ?? path.join(OUT_DIR, "shots");
const JSONL = path.join(OUT_DIR, `crawl-${DATE}.jsonl`);
const JSON_OUT = path.join(OUT_DIR, `crawl-${DATE}.json`);
const ROLES = String(args.roles ?? "admin,member").split(",").filter(Boolean);
const PAGES = args.pages ? String(args.pages).split(",") : null;
const KINDS = args.kinds ? new Set(String(args.kinds).split(",")) : null;
const LIMIT = args.limit ? Number(args.limit) : Infinity;
const FIXTURES = args.fixtures ?? "create";
const EFFECT_TIMEOUT_MS = 5000;
const MEMBER_USER = "x16m-member";
const MEMBER_PASS = process.env.X16M_MEMBER_PASSWORD ?? `X16m-${crypto.randomBytes(9).toString("base64url")}`; // memory only
const FIXTURE_PREFIX = "x16m";

fs.mkdirSync(OUT_DIR, { recursive: true });
fs.mkdirSync(SHOTS, { recursive: true });

const inventory = JSON.parse(fs.readFileSync(path.join(REPO, "frontend/src/v2/__inventory__/controls.json"), "utf8"));
const STATIC_TESTIDS = [...new Set(inventory.controls.map((c) => c.testId).filter((t) => t && !t.includes("{")))];
const en = JSON.parse(fs.readFileSync(path.join(REPO, "frontend/src/locales/en/common.json"), "utf8"));

const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const redact = (s) => String(s).replace(/"(password|passwd|secret|api_key|apikey|token|key)"\s*:\s*"[^"]*"/gi, '"$1":"[redacted]"');

/* ───────────────────────────── i18n ───────────────────────────── */
function tr(key) {
  let cur = en;
  for (const part of key.split(".")) {
    if (cur && typeof cur === "object" && part in cur) cur = cur[part];
    else return null;
  }
  return typeof cur === "string" ? cur : null;
}
/** Visible text for a label such as `t:v2.common.delete` → "Delete" (first literal fragment before any `{{…}}`). */
function labelText(label) {
  if (!label) return null;
  if (label.startsWith("t:")) {
    const key = label.slice(2).split("|")[0];
    const s = tr(key.replace(/\{[^}]*\}/g, "")) ?? null;
    if (!s) return null;
    return s.split("{{")[0].trim() || null;
  }
  if (label.startsWith("{")) return null;
  return label;
}

/* ───────────────────────────── auth / api ───────────────────────────── */
async function login(context, username, password) {
  const page = await context.newPage();
  await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
  const user = page.locator("#auth-username");
  await user.waitFor({ timeout: 30_000 });
  await user.fill(username);
  await page.locator("#auth-password").fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).last().click();
  await user.waitFor({ state: "hidden", timeout: 45_000 });
  await page.locator('[data-testid="v2-shell"]').waitFor({ timeout: 45_000 });
  const st = await (await page.request.get(`${BASE}/api/auth/status`)).json();
  if (!st.authenticated) throw new Error(`login as ${username} failed`);
  return { page, status: st };
}
async function api(page, method, p, body) {
  const res = await page.request.fetch(`${BASE}${p}`, { method, data: body, headers: { "Content-Type": "application/json" } });
  const text = await res.text();
  let json = null;
  try { json = JSON.parse(text); } catch { /* not json */ }
  return { status: res.status(), json, text };
}

/* ───────────────────────────── fixtures ───────────────────────────── */
const fixtures = { ids: {}, created: [] };
async function createFixtures(admin) {
  const need = async (label, method, p, body, pick) => {
    const r = await api(admin, method, p, body);
    if (r.status >= 300) {
      log(`fixture ${label}: ${method} ${p} → ${r.status} ${redact(r.text).slice(0, 200)}`);
      return null;
    }
    const id = pick(r.json);
    fixtures.created.push({ label, id, method, path: p });
    log(`fixture ${label} = ${id}`);
    return id;
  };
  // member account: public registration (pending) + admin approval on the default workspace
  const existing = (await api(admin, "GET", "/api/users?limit=200")).json?.items?.find((u) => u.username === MEMBER_USER);
  if (existing) {
    // a stale account from an interrupted run: reset the password so this run can sign in
    await api(admin, "PATCH", `/api/users/${existing.id}`, { password: MEMBER_PASS, status: "active", unlock: true });
    fixtures.ids.memberUserId = existing.id;
    log(`fixture member reused ${existing.id}`);
  } else {
    const reg = await api(admin, "POST", "/api/auth/register", { username: MEMBER_USER, email: `${MEMBER_USER}@example.invalid`, password: MEMBER_PASS });
    if (reg.status >= 300) log(`member register → ${reg.status} ${redact(reg.text).slice(0, 200)}`);
    const u = (await api(admin, "GET", "/api/users?limit=200")).json?.items?.find((x) => x.username === MEMBER_USER);
    if (u) {
      const ap = await api(admin, "PATCH", `/api/users/${u.id}`, { status: "active", workspace_ids: ["default"] });
      log(`member approve → ${ap.status}`);
      fixtures.ids.memberUserId = u.id;
      fixtures.created.push({ label: "member", id: u.id, method: "DELETE", path: `/api/users/${u.id}` });
    }
  }
  fixtures.ids.datasetId = await need("dataset", "POST", "/api/eval/datasets", { name: `${FIXTURE_PREFIX}-dataset`, description: "r16-m control crawl fixture", items: [{ prompt: "Say hello.", expected: "hello" }] }, (j) => j.dataset_id ?? j.id);
  fixtures.ids.evaluatorId = await need("evaluator", "POST", "/api/eval/evaluators", { name: `${FIXTURE_PREFIX}_evaluator`, base_evaluator_id: "Builtin.Helpfulness", model_id: "global.zai.glm-5.3", level: "TRACE", description: "r16-m control crawl fixture" }, (j) => j.evaluator_id ?? j.id);
  fixtures.ids.kbId = await need("kb", "POST", "/api/knowledge-bases", { name: `${FIXTURE_PREFIX}-kb`, description: "r16-m control crawl fixture", source: { mode: "upload" } }, (j) => j.id ?? j.kb_id ?? j.knowledge_base_id);
  fixtures.ids.memoryId = await need("memory", "POST", "/api/memory/resources", { name: `${FIXTURE_PREFIX}_memory`, description: "r16-m control crawl fixture", strategies: ["semantic"] }, (j) => j.id ?? j.memory_id);
  fixtures.ids.announcementId = await need("announcement", "POST", "/api/announcements", { title: `${FIXTURE_PREFIX} announcement draft`, body: "r16-m control crawl fixture (draft, never published)" }, (j) => j.id);
  fixtures.ids.registryRecordId = await need("registry", "POST", "/api/registry/records", { type: "MCP", name: `${FIXTURE_PREFIX}-mcp`, description: "r16-m control crawl fixture", url: "https://example.invalid/mcp" }, (j) => j.record_id ?? j.id);
  if (args.deploy) {
    const r = await api(admin, "POST", "/api/agents", { name: `${FIXTURE_PREFIX}-agent`, method: "harness", system_prompt: "Answer concisely." });
    if (r.status === 202) {
      fixtures.ids.agentId = r.json.agent.id;
      fixtures.ids.agentJob = r.json.job_id;
      fixtures.created.push({ label: "agent", id: r.json.agent.id, method: "DELETE", path: `/api/agents/${r.json.agent.id}` });
      log(`fixture agent ${r.json.agent.id} deploying (job ${r.json.job_id})`);
    } else log(`agent deploy → ${r.status} ${redact(r.text).slice(0, 200)}`);
  }
  if (!fixtures.ids.agentId) {
    const agents = (await api(admin, "GET", "/api/agents")).json?.agents ?? [];
    const a = agents.find((x) => x.status === "active" && !x.system) ?? agents.find((x) => x.status === "active") ?? agents[0];
    if (a) {
      fixtures.ids.agentId = a.id;
      fixtures.ids.agentName = a.name;
      fixtures.ids.agentReused = true;
      log(`agent fixture: reusing ${a.name} (${a.id}) for read-only controls`);
    }
  } else {
    fixtures.ids.agentName = `${FIXTURE_PREFIX}-agent`;
  }
  fs.writeFileSync(path.join(OUT_DIR, `fixtures-${DATE}.json`), JSON.stringify({ ids: fixtures.ids, created: fixtures.created }, null, 2));
}
async function deleteFixtures(admin) {
  const saved = fs.existsSync(path.join(OUT_DIR, `fixtures-${DATE}.json`)) ? JSON.parse(fs.readFileSync(path.join(OUT_DIR, `fixtures-${DATE}.json`), "utf8")) : { created: fixtures.created };
  const results = [];
  // discover by prefix too (an interrupted run may have lost the ledger)
  // list shapes differ per kind: take the first array in the body and the first id-like / name-like field
  const arr = (j) => (Array.isArray(j) ? j : Object.values(j ?? {}).find(Array.isArray) ?? []);
  const idOf = (d) => d.id ?? d.kb_id ?? d.knowledge_base_id ?? d.record_id ?? d.announcement_id ?? d.memory_id ?? d.dataset_id ?? d.evaluator_id;
  const nameOf0 = (d) => d.name ?? d.title ?? d.username;
  const sweeps = [
    ["dataset", "/api/eval/datasets", arr, (d) => `/api/eval/datasets/${idOf(d)}?force=true`, nameOf0],
    ["evaluator", "/api/eval/evaluators", arr, (d) => `/api/eval/evaluators/${idOf(d)}?force=true`, nameOf0],
    ["kb", "/api/knowledge-bases", arr, (d) => `/api/knowledge-bases/${idOf(d)}`, nameOf0],
    ["memory", "/api/memory/resources", arr, (d) => `/api/memory/resources/${idOf(d)}`, nameOf0],
    ["announcement", "/api/announcements/manage?limit=100", arr, (d) => `/api/announcements/${idOf(d)}`, nameOf0],
    ["registry", "/api/registry/records", arr, (d) => `/api/registry/records/${idOf(d)}`, nameOf0],
    ["agent", "/api/agents", arr, (d) => `/api/agents/${idOf(d)}`, nameOf0],
    ["user", "/api/users?limit=200", arr, (d) => `/api/users/${idOf(d)}`, nameOf0],
  ];
  for (const [label, listPath, items, delPath, nameOf] of sweeps) {
    const r = await api(admin, "GET", listPath);
    for (const d of items(r.json ?? {})) {
      const name = String(nameOf(d) ?? "");
      if (!name.startsWith(FIXTURE_PREFIX)) continue;
      const del = await api(admin, "DELETE", delPath(d));
      results.push({ label, name, status: del.status });
      log(`teardown ${label} ${name} → ${del.status}${del.status >= 300 ? ` ${redact(del.text).slice(0, 160)}` : ""}`);
    }
  }
  // verify nothing x16m- is left
  const left = [];
  for (const [label, listPath, items, , nameOf] of sweeps) {
    const r = await api(admin, "GET", listPath);
    for (const d of items(r.json ?? {})) if (String(nameOf(d) ?? "").startsWith(FIXTURE_PREFIX)) left.push(`${label}:${nameOf(d)}`);
  }
  fs.writeFileSync(path.join(OUT_DIR, `teardown-${DATE}.json`), JSON.stringify({ results, left, saved: saved.created }, null, 2));
  log(`teardown done; left: ${left.length ? left.join(", ") : "none"}`);
  return left;
}

/* ───────────────────────────── URL per control ───────────────────────────── */
function urlFor(row) {
  const ids = fixtures.ids;
  const page = row.page === "/v2/*" ? "/v2/agents" : row.page === "/v2/" ? "/v2" : row.page;
  const byPage = {
    "/v2/agents": ids.agentId, "/v2/chat": ids.agentId, "/v2/eval/data": ids.datasetId, "/v2/eval/evaluators": ids.evaluatorId,
    "/v2/knowledge-bases": ids.kbId, "/v2/memory": ids.memoryId, "/v2/announcements": ids.announcementId, "/v2/registry": ids.registryRecordId,
    "/v2/users": ids.memberUserId, "/v2/workspaces": "default",
  };
  const pageId = byPage[page];
  const tabQ = row.tab ? `tab=${encodeURIComponent(row.tab)}&` : "";
  if (!row.view) {
    if (page === "/v2/chat" && pageId) return `${page}?agent=${encodeURIComponent(pageId)}`;
    return tabQ ? `${page}?${tabQ.slice(0, -1)}` : page;
  }
  const v = row.view;
  // which fixture id a view takes — only where the fixture IS that kind of row
  const idForView = () => {
    if (/^(detail|edit|identity)$/.test(v)) return pageId; // the page's own kind
    if (/^(dataset|dataset-edit)$/.test(v)) return ids.datasetId;
    if (/^(resource|resource-edit)$/.test(v)) return ids.memoryId;
    if (v === "agent") return ids.agentId;
    return null; // trace / session / pipeline / gen / compare / policy / gateway / rate-limit: no fixture of that kind
  };
  const needsId = /detail|edit|identity|resource$|resource-edit|dataset$|dataset-edit|session|trace|gateway|policy|agent|pipeline$|gen$|compare|rate-limit/.test(v);
  if (!needsId) return `${page}?${tabQ}view=${v}`;
  const id = idForView();
  if (id) return `${page}?${tabQ}view=${v}&id=${encodeURIComponent(id)}`;
  return null; // a view that needs a row id the fixtures do not provide (conversation, trace, session, canary, …)
}

/* ───────────────────────────── locating ───────────────────────────── */
/** Candidate locators in preference order (exact testId → prefix → exact label → loose label). */
function locatorsFor(page, row) {
  if (row.testId) {
    if (row.testId.includes("{")) {
      const prefix = row.testId.split("{")[0];
      if (prefix.length < 4) return [];
      // `v2-online-{id}` must not match the static `v2-online-new` next to it
      const siblings = STATIC_TESTIDS.filter((t) => t.startsWith(prefix) && t !== prefix);
      const not = siblings.map((t) => `:not([data-testid="${t}"])`).join("");
      // and only an element that is itself clickable (a `v2-online-kpi-…` Tag shares the prefix with `v2-online-{id}`)
      // an <input> (a SearchInput shares `v2-kb-` with `v2-kb-{name}`) only counts for toggle controls
      const clickable = row.kind === "toggle" ? "button, a, [role=\"button\"], [role=\"tab\"], input, summary, select, label, [tabindex]" : "button, a, [role=\"button\"], [role=\"tab\"], summary, [tabindex]";
      return [page.locator(`[data-testid^="${prefix}"]${not}:is(${clickable})`)];
    }
    return [page.locator(`[data-testid="${row.testId}"]`)];
  }
  if (row.tag === "Pager") return [page.locator(".v2-pager button")];
  if (row.tag === "Steps") return [page.locator(".v2-steps button")];
  const text = labelText(row.label);
  if (!text) return [];
  const esc = text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const whole = new RegExp(`^\\s*${esc}\\s*$`, "i"); // case-insensitive whole name ("View" ≠ "Overview")
  const word = new RegExp(`(^|\\s)${esc}(\\s|$)`, "i"); // the label as a whole word inside a longer name
  if (row.kind === "nav-link") return [page.getByRole("link", { name: whole }), page.getByRole("link", { name: word })];
  if (row.kind === "tab") return [page.getByRole("button", { name: whole }), page.getByRole("tab", { name: whole }), page.getByRole("button", { name: word })];
  // FilterSelect renders `{label}<b>{value}</b>` with no separator ("LocaleAll"): match the name's start too
  const starts = new RegExp(`^\\s*${esc}`, "i");
  if (row.kind === "toggle") return [page.getByLabel(whole), page.getByRole("button", { name: whole }), page.getByLabel(word), page.getByRole("button", { name: starts })];
  return [page.getByRole("button", { name: whole }), page.getByRole("button", { name: word })];
}

/* ───────────────────────────── effect matching ───────────────────────────── */
function routeRegex(detail) {
  // "DELETE /api/agents/{id}" → {method, re}
  const [method, p] = detail.split(" ");
  const re = new RegExp(`^${p.replace(/[.*+?^$()|[\]\\]/g, "\\$&").replace(/\{[^}]*\}/g, "[^/?]+").replace(/\\\|/g, "|")}(\\?.*)?$`);
  return { method: method === "FETCH" ? null : method, re };
}
function navMatches(detail, before, after) {
  if (after === before) return false;
  const a = new URL(after);
  const d = detail.replace(/\{[^}]*\}/g, "");
  if (d === "history:back") return true;
  if (d.startsWith("http")) return a.href.startsWith(d.split("{")[0]);
  const [pathPart, query] = d.split("?");
  const pathOk = !pathPart || a.pathname === pathPart || a.pathname.startsWith(pathPart.replace(/\/$/, ""));
  if (!pathOk) return false;
  if (!query) return true;
  const want = new URLSearchParams(query);
  for (const [k, v] of want) {
    if (!v || v.startsWith("|")) continue; // hole
    const have = a.searchParams.get(k);
    if (have === null) return false;
    if (v.includes("|") ? !v.split("|").includes(have) : have !== v) return false;
  }
  return true;
}

/* ───────────────────────────── crawl one control ───────────────────────────── */
async function domSignature(page) {
  return page.evaluate(() => {
    const root = document.querySelector(".v2") ?? document.body;
    const dialogs = document.querySelectorAll('[role="dialog"]').length;
    const toasts = document.querySelectorAll('[data-testid="v2-toast"]').length;
    const popups = document.querySelectorAll('[data-testid="v2-select-popup"], [role="listbox"], [role="menu"], details[open]').length;
    // state flips that change neither text nor element count: checkboxes, pressed / selected / expanded
    // toggles, class-driven selection (OptionCard, Segmented, SubTabs) and disabled transitions
    const states = `${root.querySelectorAll(":checked").length}|${root.querySelectorAll('[aria-pressed="true"],[aria-selected="true"],[aria-expanded="true"],[aria-current]').length}|${root.querySelectorAll(":disabled").length}`;
    // form values are not text nodes: an "insert token" / "clear" / "pick all" shows up only here
    let values = 0;
    for (const el of root.querySelectorAll("input, textarea, select")) values += String(el.value ?? "").length + (el.checked ? 1 : 0);
    let h = 0;
    for (const el of root.querySelectorAll("[class]")) { const c = el.className; for (let i = 0; i < c.length; i++) h = (h * 31 + c.charCodeAt(i)) | 0; }
    return `${root.querySelectorAll("*").length}|${root.innerText.length}|${dialogs}|${toasts}|${popups}|${states}|${values}|${h}|${location.href}`;
  }).catch(() => "");
}

async function crawlControl(page, row, role, attempt = 0, opener = null) {
  const started = Date.now();
  const url = urlFor(opener ?? row);
  const out = { id: row.id, role, page: row.page, tab: row.tab ?? null, view: row.view, url, testId: row.testId, kind: row.kind, destructive: row.destructive, expected: row.effect, status: null, detail: null, ms: 0, shot: null, attempt, via: opener ? (opener.testId ?? opener.id) : null };
  if (!url) { out.status = "NOT-FOUND"; out.detail = `view=${(opener ?? row).view} needs a row id the fixtures do not provide`; return out; }
  const expectAbsent = role === "member" && row.roles.length === 1 && row.roles[0] === "admin";

  // navigate (fresh state for every control)
  const resp = await page.goto(`${BASE}${url}`, { waitUntil: "domcontentloaded" }).catch((e) => ({ error: e.message }));
  if (resp?.error) { out.status = "ERROR"; out.detail = `goto: ${resp.error}`; return out; }
  await page.waitForLoadState("networkidle", { timeout: 2500 }).catch(() => {}); // pages that poll never go idle
  await sleep(300);
  if (opener) {
    // drive the page into the state the opener produces (a dialog, a sub-form, a view switch)
    const olocs = locatorsFor(page, opener);
    let ot = null;
    for (const l of olocs) { const c = await l.count().catch(() => 0); for (let i = 0; i < Math.min(c, 4) && !ot; i++) if (await l.nth(i).isVisible().catch(() => false)) ot = l.nth(i); if (ot) break; }
    if (!ot) { out.status = "NOT-FOUND"; out.detail = `opener ${opener.testId ?? opener.id} not visible`; return out; }
    await ot.click({ timeout: 3000 }).catch(() => {});
    await page.waitForLoadState("networkidle", { timeout: 3000 }).catch(() => {});
    await sleep(600);
  }
  const landed = page.url();
  if (!opener && !landed.includes(url.split("?")[0].replace(/\/$/, "")) && !landed.endsWith(url)) {
    // redirected away (forbidden page for the member, or a classic → v2 bounce)
    if (expectAbsent || (role === "member" && /notfound|forbidden|\/v2\/?$/.test(landed) && row.roles.length === 1)) { out.status = "ABSENT-OK"; out.detail = `page redirected to ${new URL(landed).pathname}`; return out; }
  }

  const locs = locatorsFor(page, row);
  if (!locs.length) { out.status = "NOT-FOUND"; out.detail = "no selector (no testId and no resolvable label)"; return out; }
  let target = null;
  let count = 0;
  const prefersUnselected = ["option", "tab", "toggle"].includes(row.kind);
  for (const loc of locs) {
    const c = await loc.count().catch(() => 0);
    count += c;
    let firstVisible = null;
    for (let i = 0; i < Math.min(c, 8) && !target; i++) {
      const cand = loc.nth(i);
      if (!(await cand.isVisible().catch(() => false))) continue;
      firstVisible ??= cand;
      if (!prefersUnselected) { target = cand; break; }
      // clicking the already-selected option / tab is a no-op by design: pick another one when there is one
      const selected = await cand.evaluate((el) => el.matches(':checked, [aria-pressed="true"], [aria-selected="true"], [aria-current], .on, .active, .selected, .is-active') || el.closest('[aria-selected="true"], .on, .active, .selected') !== null).catch(() => false);
      if (!selected) { target = cand; break; }
    }
    if (!target && firstVisible) target = firstVisible;
    if (target) break;
  }
  if (!target) {
    out.status = expectAbsent ? "ABSENT-OK" : "NOT-FOUND";
    out.detail = expectAbsent ? "admin-only control absent for member" : `not visible on ${url} (${count} matches in DOM)`;
    return out;
  }
  if (expectAbsent) { out.status = "ROLE-LEAK"; out.detail = "admin-only control visible to member"; out.shot = await shot(page, row, role); return out; }

  // disabled → hint contract, no click
  const disabled = await target.evaluate((el) => el.matches(":disabled, [aria-disabled='true']") || el.closest("button:disabled, [aria-disabled='true']") !== null).catch(() => false);
  if (disabled) {
    const hint = await target.evaluate((el) => el.getAttribute("title") || el.getAttribute("aria-label") || el.closest("[title]")?.getAttribute("title") || el.getAttribute("aria-describedby") || "").catch(() => "");
    // a pager's prev/next at the first/last page and a wizard's future steps are disabled by construction
    const byConstruction = row.tag === "Pager" || row.tag === "Steps" || /^t:v2\.common\.(prev|next|back)$/.test(row.label ?? "");
    out.status = hint || byConstruction ? "DISABLED-OK" : "DISABLED-NO-HINT";
    out.detail = hint ? `disabled, hint: ${hint.slice(0, 80)}` : "disabled without a reason tooltip";
    if (!hint) out.shot = await shot(page, row, role);
    return out;
  }

  const exp = row.effect;
  const isExternal = row.kind === "nav-link" && exp && /^https?:/.test(exp.detail);
  if (isExternal) {
    const href = await target.getAttribute("href").catch(() => null);
    out.status = href ? "OK" : "DEAD"; out.detail = `external link href=${href}`; return out;
  }
  // ANY direct mutation (no Confirm in between) is fired only on our own fixture rows — whether or not the
  // inventory flagged it destructive (an enable / disable toggle, a role save, an approve …): the crawler must
  // never change shared state on the target. Read-like POSTs (search, preview, simulate, probe, …) are exempt.
  // (`stop` ends a real runtime session and `logout` ends the crawler's own: both go through the fixture gate)
  const mutating = (e) => e && e.type === "api" && /^(DELETE|POST|PUT|PATCH) /.test(e.detail) && !/\/(search|preview|simulate|inspect|probe|query|retrieve|export|compare|validate|dry-run|discovery|login|recheck)\b/.test(e.detail);
  const directMutation = mutating(exp) || (row.effects ?? []).some(mutating);
  // the member account is a fixture too, but disabling / resetting it mid-crawl would end the member session
  const ownFixture = row.page !== "/v2/users" && ((row.testId ?? "").includes(FIXTURE_PREFIX) || (await target.evaluate((el) => (el.closest("tr, li, article, section")?.textContent ?? "").includes("x16m")).catch(() => false)));
  if (directMutation && !ownFixture) { out.status = "SKIPPED-DESTRUCTIVE"; out.detail = `direct ${(mutating(exp) ? exp : (row.effects ?? []).find(mutating)).detail} on a non-fixture row — not clicked`; return out; }
  if (row.kind === "dialog-action" && row.testId === "v2-confirm-ok") { out.status = "CANCELLED"; out.detail = "Confirm OK is never clicked; covered by the opener's cancel path"; return out; }

  const wasSelected = prefersUnselected && (await target.evaluate((el) => el.matches(':checked, [aria-pressed="true"], [aria-selected="true"], [aria-current], .on, .active, .selected, .is-active') || el.closest('[aria-selected="true"], .on, .active, .selected') !== null).catch(() => false));
  // capture
  const requests = [];
  const errors = [];
  const onReq = (r) => { if (r.url().startsWith(BASE)) requests.push({ method: r.method(), path: r.url().slice(BASE.length), status: null }); };
  const onRes = (r) => { const hit = requests.find((q) => q.status === null && q.path === r.url().slice(BASE.length) && q.method === r.request().method()); if (hit) hit.status = r.status(); };
  const onConsole = (m) => { if (m.type() === "error") errors.push(`console: ${m.text().slice(0, 200)}`); };
  const onPageError = (e) => errors.push(`pageerror: ${String(e.message ?? e).slice(0, 200)}`);
  let chooser = false;
  const onChooser = () => { chooser = true; };
  page.on("request", onReq); page.on("response", onRes); page.on("console", onConsole); page.on("pageerror", onPageError); page.on("filechooser", onChooser);
  const before = await domSignature(page);
  const urlBefore = page.url();
  let clickError = null;
  try {
    await target.click({ timeout: 4000 });
  } catch (e) {
    clickError = String(e.message).split("\n")[0].slice(0, 160);
  }
  // wait for the expected effect (≤ 5 s), polling every 250 ms
  const deadline = Date.now() + EFFECT_TIMEOUT_MS;
  let matched = null;
  let after = before;
  while (Date.now() < deadline) {
    await sleep(250);
    after = await domSignature(page);
    matched = await evaluateEffect(exp, { requests, urlBefore, urlAfter: page.url(), before, after, chooser, page });
    if (matched && matched.done && matched.ok) break;
    // the inventory's primary effect is one of several the handler may produce (POST on `new`, PUT on `edit`,
    // a list GET vs a detail GET from a shared hook): any api / navigate effect of the row counts
    for (const alt of (row.effects ?? []).filter((e) => e !== exp && (e.type === "api" || e.type === "navigate"))) {
      const m2 = await evaluateEffect(alt, { requests, urlBefore, urlAfter: page.url(), before, after, chooser, page });
      if (m2 && m2.done && m2.ok) { matched = { ...m2, detail: `${m2.detail} (alternate effect ${alt.type}:${alt.detail})` }; break; }
    }
    if (matched && matched.done && matched.ok) break;
  }
  page.off("request", onReq); page.off("response", onRes); page.off("console", onConsole); page.off("pageerror", onPageError); page.off("filechooser", onChooser);

  const apiHits = requests.filter((r) => r.path.startsWith("/api/"));
  const failed = apiHits.filter((r) => r.status !== null && r.status >= 400 && !(r.status === 404 && /\/api\/(traces|chat\/[^/]+\/(memory|history))/.test(r.path)));
  const changed = after !== before || page.url() !== urlBefore || apiHits.length > 0 || chooser;
  out.network = apiHits.slice(0, 8).map((r) => `${r.method} ${r.path.split("?")[0]} ${r.status ?? "…"}`);
  out.errors = errors.slice(0, 4);

  if (clickError && !changed) { out.status = "DEAD"; out.detail = `click failed: ${clickError}`; }
  else if (!changed && wasSelected) { out.status = "ALREADY-SELECTED"; out.detail = "the only candidate was already selected; clicking it is a no-op by design"; }
  else if (!changed) { out.status = "DEAD"; out.detail = "no network, URL, dialog or DOM change within 5 s"; }
  else if (failed.length && failed.every((r) => [403, 409, 422].includes(r.status)) && (await page.locator('[role="dialog"], [data-testid="v2-toast"]').first().isVisible().catch(() => false))) {
    // the server refused with a business rule (in_use / forbidden / validation) and the UI surfaced it
    out.status = "REFUSED"; out.detail = `${failed.map((r) => `${r.method} ${r.path.split("?")[0]} → ${r.status}`).join("; ")} — surfaced by the UI (dialog / toast)`;
  }
  else if (failed.length) { out.status = "BROKEN"; out.detail = `${failed.map((r) => `${r.method} ${r.path.split("?")[0]} → ${r.status}`).join("; ")}`; }
  else if (errors.length) { out.status = "BROKEN"; out.detail = errors[0]; }
  else if (!exp) { out.status = "OK"; out.detail = "no expected effect in the inventory; something changed"; }
  else if (matched && matched.ok) { out.status = "OK"; out.detail = matched.detail; }
  else if (exp.type === "api" && /^(POST|PUT|PATCH) /.test(exp.detail) && apiHits.length === 0 && page.url() === urlBefore && after !== before) {
    // a save / submit on an incomplete form: the UI refused client-side (validation copy, no request)
    out.status = "VALIDATION"; out.detail = `no request; form showed client-side validation instead of ${exp.detail}`;
  }
  else if (exp.type === "navigate" && /^(new|.*-new|register|edit|.*-edit)$/.test(row.view ?? "") && /save|submit|create|register|publish|deploy|next|finish/i.test(`${row.testId ?? ""} ${labelText(row.label) ?? ""}`) && apiHits.filter((r) => r.method !== "GET").length === 0 && page.url() === urlBefore && after !== before) {
    // a create / register form's submit on an empty form: validation copy, no navigation yet
    out.status = "VALIDATION"; out.detail = `no mutation and no navigation; the empty form showed client-side validation instead of ${exp.detail}`;
  }
  else if (row.kind === "toggle" && (await page.locator('[data-testid="v2-select-popup"], [role="listbox"]').first().isVisible().catch(() => false))) {
    // a Select / FilterSelect trigger: the click opens the listbox; the inventory's effect fires on choosing an option
    out.status = "OK"; out.detail = `listbox opened (the ${exp.type} effect fires on option choice, not on the trigger)`;
  }
  else { out.status = "MISLABELED"; out.detail = `expected ${exp.type}:${exp.detail}; observed ${describeObserved({ apiHits, urlBefore, urlAfter: page.url(), before, after, chooser })}`; }

  // destructive → the Confirm must be cancelled; any open dialog is closed before the next control
  const dialogOpen = await page.locator('[role="dialog"]').first().isVisible().catch(() => false);
  if (dialogOpen) {
    await page.keyboard.press("Escape").catch(() => {});
    await sleep(200);
    const still = await page.locator('[role="dialog"]').first().isVisible().catch(() => false);
    if (still) {
      const cancel = page.locator('[role="dialog"] button:has-text("Cancel"), [role="dialog"] button:has-text("Close"), [role="dialog"] button[aria-label="Close"]').first();
      await cancel.click({ timeout: 1500 }).catch(() => {});
    }
    if (row.destructive) out.detail += " (Confirm cancelled)";
  }
  if (!["OK", "VALIDATION", "ALREADY-SELECTED", "REFUSED", "ABSENT-OK", "CANCELLED", "DISABLED-OK", "SKIPPED-DESTRUCTIVE"].includes(out.status)) out.shot = await shot(page, row, role);
  out.ms = Date.now() - started;
  return out;
}

function describeObserved({ apiHits, urlBefore, urlAfter, before, after, chooser }) {
  const parts = [];
  if (apiHits.length) parts.push(`api: ${apiHits.slice(0, 3).map((r) => `${r.method} ${r.path.split("?")[0]}`).join(", ")}`);
  if (urlAfter !== urlBefore) parts.push(`url: ${urlAfter.replace(BASE, "")}`);
  if (chooser) parts.push("file chooser");
  if (after !== before) {
    const [, , d0] = before.split("|"); const [, , d1] = after.split("|");
    parts.push(d1 !== d0 ? "dialog opened" : "dom changed");
  }
  return parts.join("; ") || "nothing";
}

async function evaluateEffect(exp, ctx) {
  if (!exp) return ctx.after !== ctx.before || ctx.urlAfter !== ctx.urlBefore || ctx.requests.length ? { done: true, ok: true } : null;
  const { requests, urlBefore, urlAfter, before, after, chooser } = ctx;
  if (exp.type === "api") {
    const { method, re } = routeRegex(exp.detail);
    const hit = requests.find((r) => (!method || r.method === method) && re.test(r.path));
    if (hit) return hit.status === null ? { done: false } : { done: true, ok: true, detail: `${hit.method} ${hit.path.split("?")[0]} → ${hit.status}` };
    if (/^GET /.test(exp.detail)) {
      // list vs detail GET of one resource family from a shared reload hook
      const family = exp.detail.split(" ")[1].split("/").slice(0, 3).join("/");
      const fam = requests.find((r) => r.method === "GET" && r.path.startsWith(family) && r.status !== null);
      if (fam) return { done: true, ok: true, detail: `${fam.method} ${fam.path.split("?")[0]} → ${fam.status} (same resource family as ${exp.detail})` };
      // a GET the page already made may be served from state; accept a DOM change too
      if (after !== before) return { done: true, ok: true, detail: "DOM changed (GET may have been satisfied from state)" };
    }
    return null;
  }
  if (exp.type === "navigate") {
    if (navMatches(exp.detail, urlBefore, urlAfter)) return { done: true, ok: true, detail: `url → ${urlAfter.replace(BASE, "")}` };
    if (/^\{/.test(exp.detail) && urlAfter !== urlBefore) return { done: true, ok: true, detail: `url → ${urlAfter.replace(BASE, "")} (pattern unresolved)` };
    return null;
  }
  if (exp.type === "dialog") {
    const d = exp.detail;
    // `AddToDatasetModal@Traces.tsx:197` — a component the generator could not name by testId: any dialog or DOM change counts
    if (d.includes("@")) {
      const anyDialog = await ctx.page.locator('[role="dialog"]').first().isVisible().catch(() => false);
      if (anyDialog) return { done: true, ok: true, detail: `a dialog opened (${d})` };
      if (after !== before) return { done: true, ok: true, detail: `DOM changed (${d} has no testId to assert)` };
      return null;
    }
    const sel = /^[a-z0-9-]+$/i.test(d) ? `[data-testid="${d}"]` : '[role="dialog"]';
    const visible = await ctx.page.locator(sel).first().isVisible().catch(() => false);
    if (visible) return { done: true, ok: true, detail: `dialog ${d} opened` };
    // a different dialog opened → MISLABELED after the deadline, but stop waiting now
    const other = await ctx.page.locator('[role="dialog"]').first().isVisible().catch(() => false);
    if (other) {
      const tid = await ctx.page.locator('[role="dialog"]').first().getAttribute("data-testid").catch(() => null);
      return { done: true, ok: false, detail: `dialog ${tid ?? "(no testid)"} opened instead of ${d}` };
    }
    return null;
  }
  if (exp.type === "local") {
    if (/file-picker/.test(exp.detail)) return chooser ? { done: true, ok: true, detail: "file chooser opened" } : null;
    if (after !== before || urlAfter !== urlBefore || requests.length) return { done: true, ok: true, detail: "DOM / url / network changed" };
    return null;
  }
  return null;
}

async function shot(page, row, role) {
  const name = `${role}--${row.page.replace(/\W+/g, "_")}--${(row.testId ?? row.id.split(":").pop()).replace(/\W+/g, "_")}.png`.slice(0, 180);
  const file = path.join(SHOTS, name);
  const leak = await page.evaluate(() => [...document.querySelectorAll("input[type=password]")].some((i) => i.value)).catch(() => false);
  if (leak) return null;
  await page.screenshot({ path: file, fullPage: false }).catch(() => null);
  return path.relative(OUT_DIR, file);
}

/** Up to 4 controls on the same page whose effect opens a dialog or switches ?view= — same file first. */
function openersFor(row, rows) {
  const dir = path.dirname(row.file);
  const topFile = `src/v2/pages/${row.page.replace(/^\/v2\//, "").split("/").map((s) => s.charAt(0).toUpperCase() + s.slice(1)).join("")}.tsx`;
  const cands = rows.filter((o) => o.page === row.page && o.id !== row.id && o.testId && !o.testId.includes("{") && o.effect
    && ((o.effect.type === "dialog") || (o.effect.type === "navigate" && /\?view=/.test(o.effect.detail) && !/\{/.test(o.effect.detail.split("?")[1] ?? "")))
    && !(o.destructive && o.effect.type === "api"));
  const score = (o) => (o.file === row.file ? 0 : path.dirname(o.file) === dir ? 1 : o.file === topFile ? 2 : 3);
  return cands.sort((a, b) => score(a) - score(b)).filter((o) => score(o) < 3).slice(0, 4);
}

/* ───────────────────────────── main ───────────────────────────── */
(async () => {
  const browser = await chromium.launch({ headless: true });
  const adminCtx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 900 }, locale: "en-US" });
  const { page: admin } = await login(adminCtx, ADMIN_USER, ADMIN_PASS);
  const health = await (await admin.request.get(`${BASE}/api/health`)).json();
  log(`console ${BASE} build ${health.build?.short}`);

  if (FIXTURES === "delete-only") {
    const left = await deleteFixtures(admin);
    await browser.close();
    process.exit(left.length ? 1 : 0);
  }
  if (FIXTURES === "create") await createFixtures(admin);
  else if (fs.existsSync(path.join(OUT_DIR, `fixtures-${DATE}.json`))) fixtures.ids = JSON.parse(fs.readFileSync(path.join(OUT_DIR, `fixtures-${DATE}.json`), "utf8")).ids;

  // member session
  let member = null;
  if (ROLES.includes("member")) {
    const memberCtx = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 900 }, locale: "en-US" });
    try { member = (await login(memberCtx, MEMBER_USER, MEMBER_PASS)).page; } catch (e) {
      // the member password lives only in the process that created the account: on a resumed run
      // the admin resets it (PATCH /api/users/{id} {password}) and signs in again
      log(`member login failed (${e.message.split("\n")[0]}); resetting the fixture member's password as admin`);
      const u = (await api(admin, "GET", "/api/users?limit=200")).json?.items?.find((x) => x.username === MEMBER_USER);
      if (u) {
        const pr = await api(admin, "PATCH", `/api/users/${u.id}`, { password: MEMBER_PASS, status: "active", unlock: true });
        log(`member password reset → ${pr.status}`);
        const ctx2 = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1440, height: 900 }, locale: "en-US" });
        try { member = (await login(ctx2, MEMBER_USER, MEMBER_PASS)).page; } catch (e2) { log(`member login failed again: ${e2.message.split("\n")[0]} — member role skipped`); }
      } else log("fixture member account not found — member role skipped");
    }
  }

  const done = new Set();
  const last = new Map(); // `${role}|${id}` → last recorded status
  if (fs.existsSync(JSONL)) for (const line of fs.readFileSync(JSONL, "utf8").split("\n")) if (line) { const r = JSON.parse(line); last.set(`${r.role}|${r.id}`, r.status); }
  if (args.resume) for (const k of last.keys()) done.add(k);
  const REDO = args.redo ? new Set(String(args.redo).split(",")) : null;

  // page order: agents / chat last so a --deploy fixture has time to finish
  const order = (p) => (p === "/v2/agents" || p === "/v2/chat" ? 2 : p === "/v2/*" ? 1 : 0);
  let rows = inventory.controls.filter((c) => c.page !== "(unrouted)" && (!PAGES || PAGES.includes(c.page)) && (!KINDS || KINDS.has(c.kind)));
  rows = [...rows].sort((a, b) => order(a.page) - order(b.page) || a.page.localeCompare(b.page) || a.line - b.line);
  const results = [];
  let n = 0;
  for (const role of ROLES) {
    const page = role === "admin" ? admin : member;
    if (!page) continue;
    for (const row of rows) {
      if (n >= LIMIT) break;
      if (REDO) {
        const prev = last.get(`${role}|${row.id}`);
        if (prev !== undefined && !REDO.has(prev)) continue; // keep every status not listed; never-crawled rows run
      } else if (done.has(`${role}|${row.id}`)) continue;
      // wait for a deploying fixture agent before the agent pages
      if (fixtures.ids.agentJob && !fixtures.ids.agentReady && (row.page === "/v2/agents" || row.page === "/v2/chat")) {
        for (let i = 0; i < 60; i++) {
          const j = (await api(admin, "GET", `/api/jobs/${fixtures.ids.agentJob}`)).json;
          if (j && !["pending", "queued", "running", "in_progress"].includes(String(j.status).toLowerCase())) { fixtures.ids.agentReady = j.status; log(`fixture agent job ${j.status}`); break; }
          await sleep(15_000);
        }
        fixtures.ids.agentReady ??= "timeout";
      }
      let r;
      try { r = await crawlControl(page, row, role); } catch (e) { r = { id: row.id, role, page: row.page, view: row.view, testId: row.testId, kind: row.kind, expected: row.effect, status: "ERROR", detail: String(e.message).slice(0, 200) }; }
      // openers only pay off for a control the crawler can actually select, on a URL it can reach
      if (REDO && r.status === "NOT-FOUND" && !/no selector|needs a row id/.test(r.detail ?? "")) {
        const tried = [];
        for (const opener of openersFor(row, rows).slice(0, 2)) {
          tried.push(opener.testId);
          let r2 = null;
          try { r2 = await crawlControl(page, row, role, 0, opener); } catch { r2 = null; }
          if (r2 && r2.status !== "NOT-FOUND") { r = r2; break; }
        }
        if (r.status === "NOT-FOUND") r.detail = `${r.detail}; openers tried: ${tried.join(", ") || "none"}`;
      }
      if (["DEAD", "BROKEN", "MISLABELED"].includes(r.status)) {
        // one retry after a reload: FLAKY if it passes
        let r2;
        try { r2 = await crawlControl(page, row, role, 1); } catch { r2 = null; }
        if (r2 && r2.status === "OK") { r = { ...r2, status: "FLAKY", detail: `first: ${r.status} (${r.detail}); retry OK` }; }
        else if (r2) r = { ...r2, shot: r2.shot ?? r.shot };
      }
      if (r.status === "ERROR" && /has been closed/.test(r.detail ?? "")) {
        // the browser is gone (killed / crashed): stop instead of recording one ERROR per remaining control
        log(`browser closed — stopping after ${n} controls; rerun with --redo=ERROR,… to continue`);
        break;
      }
      results.push(r);
      fs.appendFileSync(JSONL, `${JSON.stringify(r)}\n`);
      n += 1;
      if (n % 25 === 0) log(`${n} controls · last ${role} ${row.page} ${row.testId ?? row.id} → ${r.status}`);
    }
  }

  // aggregate
  const raw = fs.existsSync(JSONL) ? fs.readFileSync(JSONL, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : results;
  const lastBy = new Map();
  for (const r of raw) lastBy.set(`${r.role}|${r.id}`, r);
  const all = [...lastBy.values()];
  const byPageRole = {};
  for (const r of all) {
    const k = `${r.page}|${r.role}`;
    byPageRole[k] ??= {};
    byPageRole[k][r.status] = (byPageRole[k][r.status] ?? 0) + 1;
  }
  const summary = {};
  for (const r of all) summary[r.status] = (summary[r.status] ?? 0) + 1;
  fs.writeFileSync(JSON_OUT, JSON.stringify({ base: BASE, build: health.build?.short, date: DATE, roles: ROLES, fixtures: fixtures.ids, summary, byPageRole, results: all }, null, 1));
  log(`wrote ${JSON_OUT}: ${JSON.stringify(summary)}`);

  if (FIXTURES === "create" || FIXTURES === "delete") await deleteFixtures(admin);
  await browser.close();
})().catch((e) => { console.error("crawl failed:", e); process.exit(1); });

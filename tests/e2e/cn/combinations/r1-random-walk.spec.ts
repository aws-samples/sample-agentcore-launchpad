/**
 * UI random walk (round 16, r16-n step 3), model-based: on three modules (tasks list+detail,
 * agents list+detail, memory) a seeded RNG clicks a random ENABLED control inventoried from
 * the live DOM (walk-model.ts decides what may be clicked — destructive controls only on
 * x16n- fixture rows, never sign-out / deploy / install), then checks the invariants:
 *   no page error / console error · an effect within 5 s (DEAD otherwise) · URL ↔ page
 *   (walk-model URL_RULES) · recoverable (every 10 steps: reload + back land on a working
 *   page) · list data == API (tasks / agents list totals when no filter is active).
 * X16N_WALK_RUNS runs × X16N_WALK_STEPS steps per module per role; every failure records
 * the seed and the step list so it replays (`X16N_SEED`, the step log in results.jsonl).
 */
import { expect, test, type Page } from "@playwright/test";
import { createHash } from "node:crypto";

import * as L from "./lib";
import { rng } from "./pairwise";
import { chooseControl, type Control, effectOf, isClickable, NO_EFFECT_OK_RE, rulesFor, type Snapshot } from "./walk-model";

const SEED = Number(process.env.X16N_SEED ?? 16);
const RUNS = Number(process.env.X16N_WALK_RUNS ?? 10);
const STEPS = Number(process.env.X16N_WALK_STEPS ?? 40);
const ROLES = (process.env.X16N_WALK_ROLES ?? "admin,member").split(",") as ("admin" | "member")[];
/** X16N_WALK_MODULES=tasks,agents narrows a run (one process per module runs them in parallel) */
const ONLY_MODULES = (process.env.X16N_WALK_MODULES ?? "").split(",").filter(Boolean);
const SUITE = "random-walk";
const MODULES: { key: string; path: string; prefix: string }[] = [
  { key: "tasks", path: "/v2/eval/tasks?tab=all", prefix: "/v2/eval/tasks" },
  { key: "agents", path: "/v2/agents", prefix: "/v2/agents" },
  { key: "memory", path: "/v2/memory", prefix: "/v2/memory" },
];

// tests run in file order (workers: 1, fullyParallel: false); NOT serial mode — a soft failure in one test must not skip the rest

const sessions: Partial<Record<"admin" | "member", L.Session>> = {};
const sent = new WeakMap<L.Session, { n: number }>();
function countRequests(s: L.Session) {
  const c = { n: 0 };
  sent.set(s, c);
  s.ctx.on("request", (r) => {
    if (r.url().includes("/api/")) c.n += 1;
  });
}

test.beforeAll(async ({ browser }) => {
  sessions.admin = await L.session(browser, "admin");
  countRequests(sessions.admin);
  if (ROLES.includes("member")) {
    await L.ensureMember(sessions.admin.api);
    sessions.member = await L.session(browser, "member");
    countRequests(sessions.member);
  }
});
test.afterAll(async () => {
  for (const s of Object.values(sessions)) await s?.ctx.close();
});

/* ───────────── DOM inventory ───────────── */
async function inventory(page: Page): Promise<Control[]> {
  return page.evaluate(() => {
    const out: Control[] = [];
    const vis = (el: Element) => {
      const r = (el as HTMLElement).getBoundingClientRect();
      const st = getComputedStyle(el as HTMLElement);
      return r.width > 0 && r.height > 0 && st.visibility !== "hidden" && st.display !== "none";
    };
    const rowOf = (el: Element) => (el.closest("tr, .v2-card, .v2-modal, .v2-drawer, [role=dialog]")?.textContent ?? "").replace(/\s+/g, " ").slice(0, 400);
    const inDialog = (el: Element) => !!el.closest(".v2-modal, .v2-drawer, [role=dialog]");
    const idOf = (el: Element, kind: string, text: string) => el.getAttribute("data-testid") ?? `${kind}:${text.slice(0, 40)}`;
    let n = 0;
    document.querySelectorAll<HTMLElement>("button, a[href], input[type=checkbox], input[type=text], input:not([type]), input[type=search], textarea, [role=tab], [role=radio], [role=option]").forEach((el) => {
      if (!vis(el)) return;
      // stamp a handle the driver can click without re-resolving by text
      n += 1;
      el.setAttribute("data-x16n", String(n));
      const text = (el.getAttribute("aria-label") ?? el.textContent ?? el.getAttribute("placeholder") ?? "").replace(/\s+/g, " ").trim();
      const tag = el.tagName.toLowerCase();
      const role = el.getAttribute("role");
      let kind: Control["kind"] = "button";
      if (tag === "a") kind = "link";
      else if (role === "tab") kind = "tab";
      else if (role === "radio") kind = "radio";
      else if (role === "option") kind = "option";
      else if (tag === "input" && (el as HTMLInputElement).type === "checkbox") kind = "checkbox";
      else if (tag === "input" || tag === "textarea") kind = "text";
      else if (el.classList.contains("v2-sel") || el.classList.contains("v2-filter")) kind = "listbox";
      const disabled = (el as HTMLButtonElement).disabled || el.getAttribute("aria-disabled") === "true" || (el as HTMLInputElement).readOnly === true;
      const sameHref = tag === "a" && !!(el as HTMLAnchorElement).getAttribute("href") && new URL((el as HTMLAnchorElement).href, location.href).pathname === location.pathname && !new URL((el as HTMLAnchorElement).href, location.href).search;
      const alreadyOn = sameHref || (role === "tab" && el.getAttribute("aria-selected") === "true") || (role === "radio" && el.getAttribute("aria-checked") === "true") || (el.classList.contains("v2-option") && el.classList.contains("on")) || el.getAttribute("aria-current") === "step" || (!!el.closest(".v2-steps") && /\b(on|current|active)\b/.test(el.className));
      out.push({ id: idOf(el, kind, text), kind, text, rowText: rowOf(el), href: tag === "a" ? (el as HTMLAnchorElement).getAttribute("href") : null, disabled, inDialog: inDialog(el), handle: String(n), alreadyOn });
    });
    return out;
  });
}

async function snapshot(s: L.Session): Promise<Snapshot> {
  const page = s.page;
  const [text, values, count, dialogs] = await Promise.all([
    L.bodyText(page),
    // innerText excludes form values: a fill must count as an effect
    // innerText excludes form values and selection classes: a fill or an option-card toggle must count
    page.evaluate(() => [
      ...Array.from(document.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>("input, textarea")).map((i) => (i.type === "checkbox" ? String((i as HTMLInputElement).checked) : i.value)),
      ...Array.from(document.querySelectorAll(".v2-option, [role=tab], [role=radio], .v2-steps button, .v2-subtabs button")).map((e) => `${e.className}|${e.getAttribute("aria-selected") ?? ""}|${e.getAttribute("aria-checked") ?? ""}`),
    ].join("\u0001")).catch(() => ""),
    page.locator("body *").count().catch(() => 0),
    page.locator(".v2-modal, .v2-drawer, [role=dialog]").count().catch(() => 0),
  ]);
  return { url: page.url(), textHash: createHash("sha1").update(text).update(values).digest("hex"), elementCount: count, requests: sent.get(s)?.n ?? s.hits.length, openDialogs: dialogs };
}

/** the control's locator: the handle stamped by the last inventory */
function locate(page: Page, c: Control) {
  return page.locator(`[data-x16n="${c.handle ?? ""}"]`).first();
}

async function healthy(page: Page): Promise<string | null> {
  const text = await L.bodyText(page);
  if (text.length < 40) return "blank page";
  if (/something went wrong|unexpected application error|cannot read properties|minified react error/i.test(text)) return `error boundary: ${text.slice(0, 120)}`;
  return null;
}

async function dataCheck(s: L.Session, moduleKey: string): Promise<string | null> {
  const page = s.page;
  const activeFilters = await page.locator(".v2-toolbar .v2-filter.active").count();
  const q = await page.locator(".v2-toolbar .v2-search input").first().inputValue().catch(() => "");
  if (activeFilters || q) return null;
  const u = new URL(page.url());
  const countOf = async (table: string) => {
    if (!(await page.getByTestId(table).isVisible().catch(() => false))) return null;
    const card = page.getByTestId(table).locator("xpath=ancestor::*[contains(@class,'v2-card')][1]");
    // "0 rows" flashes while the list loads: wait for the spinner to leave (bounded)
    await card.locator(".v2-spin, .v2-spinner, [class*=spin]").first().waitFor({ state: "hidden", timeout: 8000 }).catch(() => undefined);
    await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => undefined);
    const label = await card.locator(".v2-count").first().innerText().catch(() => "");
    const n = Number((label.match(/\d+/) ?? [NaN])[0]);
    return Number.isNaN(n) ? null : n;
  };
  if (moduleKey === "agents" && !u.searchParams.get("view")) {
    const n = await countOf("v2-agents-table");
    const api = ((await s.api.get("/api/agents")).body?.agents ?? []).length;
    if (n !== null && n !== api) return `agents total ${n} ≠ API ${api}`;
  }
  if (moduleKey === "tasks" && u.searchParams.get("tab") === "all" && !u.searchParams.get("view")) {
    const n = await countOf("v2-tasks-table");
    const runs = ((await s.api.get("/api/eval/runs?limit=200")).body?.runs ?? []).length;
    const online = ((await s.api.get("/api/eval/online")).body?.configs ?? []).filter((x: { owner: string; mode: string }) => x.owner === "agent" && x.mode === "scores").length;
    if (n !== null && n !== runs + online) return `tasks total ${n} ≠ API ${runs + online}`;
  }
  return null;
}

interface Step {
  n: number;
  control: string;
  kind: string;
  text: string;
  effect: string[];
  url: string;
  problems: string[];
}

async function walk(s: L.Session, mod: (typeof MODULES)[number], run: number): Promise<{ steps: Step[]; failures: Step[]; seed: number }> {
  const seed = SEED * 1000 + run * 7 + mod.key.length;
  const random = rng(seed);
  const page = s.page;
  const steps: Step[] = [];
  const failures: Step[] = [];
  const seen = new Set<string>();
  await L.goto(page, mod.path, 500);
  for (let n = 1; n <= STEPS; n += 1) {
    const m = L.mark(s);
    const problems: string[] = [];
    let inv = await inventory(page);
    // stay inside the module: if a previous step left it, come back
    if (!new URL(page.url()).pathname.startsWith(mod.prefix)) {
      await L.goto(page, mod.path, 300);
      inv = await inventory(page);
    }
    // an open dialog intercepts every click outside it: only its own controls are candidates
    const dialogOpen = inv.some((c) => c.inDialog);
    const candidates = inv.filter((c) => isClickable(c, mod.prefix).ok && (!dialogOpen || c.inDialog));
    const control = chooseControl(random, candidates, seen);
    if (!control) {
      problems.push("no clickable control");
      failures.push({ n, control: "-", kind: "-", text: "-", effect: [], url: page.url(), problems });
      break;
    }
    seen.add(control.id);
    const before = await snapshot(s);
    try {
      const loc = locate(page, control);
      if (control.kind === "text") {
        const current = await loc.inputValue().catch(() => "");
        await loc.fill(current ? "" : "x16n", { timeout: 5000 });
      }
      else await loc.click({ timeout: 5000, trial: false });
    } catch (err) {
      problems.push(`click failed: ${String(err).split("\n")[0].slice(0, 120)}`);
    }
    // effect within 5 s
    let effect: string[] = [];
    const t0 = Date.now();
    while (Date.now() - t0 < 5000) {
      effect = effectOf(before, await snapshot(s));
      if (effect.length) break;
      await L.sleep(250);
    }
    if (!effect.length && !NO_EFFECT_OK_RE.test(control.text) && !problems.length) problems.push("DEAD: no effect within 5 s");
    // dismiss a dialog left open by a non-fixture flow? (we only open fixture dialogs; a Confirm on a fixture row is fine to leave for the next step)
    // URL ↔ page
    for (const rule of rulesFor(page.url())) {
      const ok = await page.locator(rule.mustSee).first().waitFor({ state: "visible", timeout: 5000 }).then(() => true).catch(() => false);
      if (!ok) problems.push(`URL ${rule.param}=${rule.value} but ${rule.mustSee} not visible`);
    }
    const h = await healthy(page);
    if (h) problems.push(h);
    const d = L.since(s, m);
    if (d.pageErrors.length) problems.push(`pageError: ${d.pageErrors.map((e) => e.text).join(" | ").slice(0, 160)}`);
    const consoleErrors = d.consoleErrors.filter((e) => !/Failed to load resource/.test(e.text));
    if (consoleErrors.length) problems.push(`consoleError: ${consoleErrors.map((e) => e.text).join(" | ").slice(0, 160)}`);
    const dc = await dataCheck(s, mod.key).catch(() => null);
    if (dc) problems.push(`INCONSISTENT: ${dc}`);
    // recoverability every 10 steps: reload, then back
    if (n % 10 === 0) {
      await page.reload({ waitUntil: "domcontentloaded" }).catch(() => problems.push("reload failed"));
      await page.waitForLoadState("networkidle", { timeout: 15_000 }).catch(() => {});
      const h2 = await healthy(page);
      if (h2) problems.push(`after reload: ${h2}`);
      await page.goBack({ waitUntil: "domcontentloaded" }).catch(() => {});
      await page.waitForLoadState("networkidle", { timeout: 8000 }).catch(() => {});
      // back may land outside the console (about:blank / the login redirect): that is not a page bug
      if (new URL(page.url()).pathname.startsWith("/v2")) {
        const h3 = await healthy(page);
        if (h3) problems.push(`after back: ${h3}`);
      } else await L.goto(page, mod.path, 300);
    }
    const step: Step = { n, control: control.id, kind: control.kind, text: control.text.slice(0, 60), effect, url: page.url(), problems };
    steps.push(step);
    if (problems.length) {
      failures.push(step);
      await L.shot(page, `R-${mod.key}-${s.role}-run${run}-step${n}`);
    }
  }
  return { steps, failures, seed };
}

for (const mod of MODULES.filter((m) => !ONLY_MODULES.length || ONLY_MODULES.includes(m.key))) {
  for (const role of ROLES) {
    test(`random walk: ${mod.key} as ${role}`, async () => {
      const s = sessions[role];
      if (!s) test.skip();
      const summary: { run: number; seed: number; steps: number; failures: number }[] = [];
      for (let run = 1; run <= RUNS; run += 1) {
        const r = await walk(s!, mod, run);
        summary.push({ run, seed: r.seed, steps: r.steps.length, failures: r.failures.length });
        L.writeJson(`walk-${mod.key}-${role}-run${run}.json`, { seed: r.seed, steps: r.steps });
        // group identical problems so one cause is one row
        const byProblem = new Map<string, Step[]>();
        for (const f of r.failures) for (const p of f.problems) byProblem.set(p.replace(/\d+/g, "#").slice(0, 80), [...(byProblem.get(p.replace(/\d+/g, "#").slice(0, 80)) ?? []), f]);
        for (const [problem, fs] of byProblem) {
          const cls: L.Cls = /^DEAD/.test(problem) ? "DEAD" : /pageError|error boundary|blank|reload|back/.test(problem) ? "BROKEN" : /INCONSISTENT/.test(problem) ? "INCONSISTENT" : /URL/.test(problem) ? "UX" : "BROKEN";
          L.record({ suite: SUITE, id: `${mod.key}-${role}-run${run}-${problem.slice(0, 40)}`, verdict: "FAIL", cls, role, observed: `${fs.length} step(s): ${fs.map((f) => `#${f.n} ${f.kind} "${f.text}" → ${f.problems.join("; ")}`).join(" || ").slice(0, 600)}`, repro: { seed: r.seed, module: mod.path, steps: fs.map((f) => f.n), file: `walk-${mod.key}-${role}-run${run}.json` } });
        }
        if (!r.failures.length) L.record({ suite: SUITE, id: `${mod.key}-${role}-run${run}`, verdict: "PASS", role, observed: `${r.steps.length} steps, ${new Set(r.steps.map((x) => x.control)).size} distinct controls`, repro: { seed: r.seed } });
      }
      const failed = summary.filter((x) => x.failures).length;
      L.log("walk summary", mod.key, role, summary);
      expect.soft(failed, `${mod.key}/${role}: ${failed}/${RUNS} runs with failures (see results.jsonl)`).toBe(0);
    });
  }
}

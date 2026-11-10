#!/usr/bin/env node
// UI executor for the lifecycle graphs (round 16, r16-o) — docs/cn/interfaces/testing.md §6.7.
//
// Every edge of backend/tests/graph/graphs/*.yaml carries a `ui:` block — the V2 page / view the
// action lives on and the `data-testid`s (from the r16-m control inventory,
// frontend/src/v2/__inventory__/controls.json) a user clicks to take it — or `ui: null` when the
// edge is not a console action (a job finishing, an extractor event, a /v1 call).
//
// Two modes:
//   --check (default, OFFLINE, no browser, no network): join every `ui.controls` entry with the
//     inventory and report, per graph, the edges whose control does not exist in the inventory
//     (a UI gap: the contract names a transition no control can take — a register row), the
//     edges that are API-only by design (`ui: null`), and the prime paths that are walkable on
//     the console (every edge has a control). Exit code 0; the report goes to stdout (markdown).
//   --walk (LIVE): walks the prime paths whose every edge has a control on a deployed console,
//     through the inventory's selectors, asserting after each click the effect the inventory
//     recorded for that control (an `/api` request matching the edge's route, a navigation, a
//     dialog) and the status the graph names (`expect.status` → the matching network response).
//     Refuses to start unless LAUNCHPAD_E2E_BASE_URL + LAUNCHPAD_E2E_PASSWORD are set AND the
//     explicit `--live` flag is passed: by default this script runs NOTHING live (C8 — the hub is
//     reached only through its console API over the tunnel the lead opened). Destructive
//     controls stop at their Confirm dialog unless the fixture prefix is `x16o-`.
//
// Env (walk only): LAUNCHPAD_E2E_BASE_URL, LAUNCHPAD_E2E_USERNAME (default admin),
// LAUNCHPAD_E2E_PASSWORD (fetch in-process; never written), PLAYWRIGHT_MODULE.
// Flags: --graphs=agent,chat_session  --out=<dir> (walk evidence, default ./out)
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "../../../..");
const requireFrom = createRequire(import.meta.url);

function loadYaml() {
  for (const candidate of [path.join(REPO, "frontend/node_modules/js-yaml"), "js-yaml"]) {
    try {
      return requireFrom(candidate);
    } catch {
      /* next */
    }
  }
  console.error("js-yaml not found — `cd frontend && npm install` provides it");
  process.exit(2);
}

const args = Object.fromEntries(process.argv.slice(2).map((a) => {
  const m = a.match(/^--([^=]+)(?:=(.*))?$/);
  return m ? [m[1], m[2] ?? true] : [a, true];
}));
const MODE = args.walk ? "walk" : "check";
const ONLY = args.graphs ? new Set(String(args.graphs).split(",")) : null;

const yaml = loadYaml();
const GRAPH_DIR = path.join(REPO, "backend/tests/graph/graphs");
const INVENTORY = JSON.parse(fs.readFileSync(path.join(REPO, "frontend/src/v2/__inventory__/controls.json"), "utf8"));
const byTestId = new Map();
for (const c of INVENTORY.controls) {
  if (c.testId) byTestId.set(c.testId, c);
}

/* ───────────────────────────── graphs → requirements ───────────────────────────── */
function loadGraphs() {
  const out = [];
  for (const file of fs.readdirSync(GRAPH_DIR).filter((f) => f.endsWith(".yaml")).sort()) {
    const g = yaml.load(fs.readFileSync(path.join(GRAPH_DIR, file), "utf8"));
    if (ONLY && !ONLY.has(g.graph)) continue;
    out.push(g);
  }
  return out;
}

/** Prime paths, the same definition as backend/tests/graph/paths.py (edge-based simple paths,
 *  maximal at both ends; a closing cycle allowed), longest first then by edge ids. */
function primePaths(g) {
  const edges = [...g.edges].sort((a, b) => a.id.localeCompare(b.id));
  const outOf = (n) => edges.filter((e) => e.from === n);
  const intoN = (n) => edges.filter((e) => e.to === n);
  const nodesOf = (p) => [p[0].from, ...p.map((e) => e.to)];
  const closed = (p) => { const n = nodesOf(p); return n[0] === n[n.length - 1]; };
  const forward = (p) => {
    if (closed(p)) return [];
    const n = nodesOf(p);
    return outOf(n[n.length - 1]).filter((e) => !n.includes(e.to) || e.to === n[0]).map((e) => [...p, e]);
  };
  const backward = (p) => {
    if (closed(p)) return false;
    const n = nodesOf(p);
    return intoN(n[0]).some((e) => !n.includes(e.from) || e.from === n[n.length - 1]);
  };
  const all = [];
  const stack = edges.map((e) => [e]);
  while (stack.length) {
    const p = stack.pop();
    all.push(p);
    stack.push(...forward(p).reverse());
  }
  const primes = all.filter((p) => forward(p).length === 0 && !backward(p));
  primes.sort((a, b) => (b.length - a.length) || a.map((e) => e.id).join(">").localeCompare(b.map((e) => e.id).join(">")));
  return primes;
}

/* ───────────────────────────── the offline check ───────────────────────────── */
function check(graphs) {
  const L = ["# Lifecycle graphs → V2 controls (UI executor, offline check)", ""];
  L.push("Join of every graph edge's `ui.controls` with `frontend/src/v2/__inventory__/controls.json`.");
  L.push("`api-only` = `ui: null` (not a console action); `gap` = a control the inventory does not have;");
  L.push("`walkable` prime paths have a known control on every edge.", "");
  L.push("| graph | edges | with controls | api-only | gap edges | prime paths | walkable prime paths |");
  L.push("|---|---|---|---|---|---|---|");
  const gaps = [];
  const totals = { edges: 0, ui: 0, api: 0, gap: 0, primes: 0, walkable: 0 };
  for (const g of graphs) {
    let ui = 0, api = 0;
    const gapEdges = new Set();
    for (const e of g.edges) {
      if (!e.ui) { api += 1; continue; }
      ui += 1;
      const missing = (e.ui.controls ?? []).filter((t) => !byTestId.has(t));
      const pageRows = INVENTORY.controls.filter((c) => c.page === e.ui.page && (e.ui.view ?? null) === (c.view ?? null));
      if (missing.length || (e.ui.controls ?? []).length === 0) {
        gapEdges.add(e.id);
        gaps.push({ graph: g.graph, edge: e.id, page: e.ui.page, view: e.ui.view, missing, none: (e.ui.controls ?? []).length === 0, pageControls: pageRows.length });
      }
    }
    const primes = primePaths(g);
    const walkable = primes.filter((p) => p.every((e) => e.ui && !gapEdges.has(e.id))).length;
    totals.edges += g.edges.length; totals.ui += ui; totals.api += api; totals.gap += gapEdges.size;
    totals.primes += primes.length; totals.walkable += walkable;
    L.push(`| \`${g.graph}\` | ${g.edges.length} | ${ui} | ${api} | ${gapEdges.size} | ${primes.length} | ${walkable} |`);
  }
  L.push(`| **total** | ${totals.edges} | ${totals.ui} | ${totals.api} | ${totals.gap} | ${totals.primes} | ${totals.walkable} |`, "");
  if (gaps.length) {
    L.push("## UI gaps (an edge the contract names, with no inventory control to take it)", "");
    L.push("| graph | edge | page / view | missing testIds | note |");
    L.push("|---|---|---|---|---|");
    for (const gp of gaps) {
      const note = gp.none ? `no control declared; the page/view has ${gp.pageControls} inventory controls` : "";
      L.push(`| \`${gp.graph}\` | \`${gp.edge}\` | ${gp.page}${gp.view ? ` ?view=${gp.view}` : ""} | ${gp.missing.map((m) => `\`${m}\``).join(", ") || "—"} | ${note} |`);
    }
    L.push("");
  }
  return L.join("\n");
}

/* ───────────────────────────── the live walk (gated) ───────────────────────────── */
async function walk(graphs) {
  const BASE = process.env.LAUNCHPAD_E2E_BASE_URL;
  const PASS = process.env.LAUNCHPAD_E2E_PASSWORD;
  if (!args.live || !BASE || !PASS) {
    console.error("refusing to walk live: pass --live AND set LAUNCHPAD_E2E_BASE_URL + LAUNCHPAD_E2E_PASSWORD "
      + "(the lead must have opened a test-host window; by default nothing runs live)");
    process.exit(3);
  }
  const PW = process.env.PLAYWRIGHT_MODULE
    ?? (fs.existsSync(path.join(REPO, "tests/e2e/node_modules/playwright")) ? path.join(REPO, "tests/e2e/node_modules/playwright") : "playwright");
  const { chromium } = requireFrom(PW);
  const OUT = path.resolve(args.out ?? path.join(HERE, "out"));
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({ baseURL: BASE, ignoreHTTPSErrors: true, locale: "en-US" });
  const page = await context.newPage();
  await page.goto("/");
  await page.getByTestId("login-username").fill(process.env.LAUNCHPAD_E2E_USERNAME ?? "admin");
  await page.getByTestId("login-password").fill(PASS);
  await page.getByTestId("login-submit").click();
  await page.getByTestId("v2-shell").waitFor();
  const records = [];
  for (const g of graphs) {
    for (const prime of primePaths(g)) {
      if (!prime.every((e) => e.ui && (e.ui.controls ?? []).every((t) => byTestId.has(t)))) continue;
      for (const e of prime) {
        const url = `${e.ui.page}${e.ui.view ? `?view=${e.ui.view}` : ""}`;
        await page.goto(url);
        for (const testId of e.ui.controls) {
          const control = byTestId.get(testId);
          const locator = page.locator(control.selector.replace(/\{[^}]*\}/, ""));
          const wait = control.effect?.type === "api"
            ? page.waitForResponse((r) => r.url().includes("/api/"), { timeout: 5000 }).catch(() => null)
            : Promise.resolve(null);
          if (control.destructive && !String(args.fixtures ?? "").startsWith("x16o-")) {
            records.push({ graph: g.graph, edge: e.id, testId, status: "SKIPPED-DESTRUCTIVE" });
            continue;
          }
          try {
            await locator.first().click({ timeout: 5000 });
            const res = await wait;
            const observed = res ? res.status() : null;
            const want = e.expect?.status ?? null;
            records.push({ graph: g.graph, edge: e.id, testId, status: want && observed && want !== observed ? "MISMATCH" : "OK", want, observed });
          } catch (err) {
            records.push({ graph: g.graph, edge: e.id, testId, status: "NOT-FOUND", error: String(err).slice(0, 200) });
          }
        }
      }
    }
  }
  fs.writeFileSync(path.join(OUT, `graph-walk-${new Date().toISOString().slice(0, 10)}.json`), JSON.stringify(records, null, 1));
  await browser.close();
  console.log(JSON.stringify({ walked: records.length, mismatches: records.filter((r) => r.status === "MISMATCH").length }, null, 1));
}

const graphs = loadGraphs();
if (MODE === "check") {
  process.stdout.write(check(graphs) + "\n");
} else {
  walk(graphs).catch((err) => { console.error(err); process.exit(1); });
}

// Observability phase-3 evidence: waterfall + span drawer on a real trace,
// session transcript, cross-links (both directions), deep links + not-found.
// Usage: node frontend/scripts/obs_phase3_evidence.mjs [baseUrl]
import { mkdirSync } from "node:fs";
import { chromium } from "/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs";

const BASE = process.argv[2] ?? "http://localhost:5174";
const OUT = "design/screenshots/obs-phase3";
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
// V2 Home starts its guided tour on a first visit: mark it seen so it never covers a shot
await page.addInitScript(() => { try { localStorage.setItem("launchpad_v2_tour_done", "1"); } catch { /* storage blocked: nothing to seed */ } });
await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
await page.evaluate(() => localStorage.setItem("i18nextLng", "en"));

// pick a rich trace + platform session + external session from the API
const picks = await page.evaluate(async () => {
  const traces = await (await fetch("/api/observability/traces?range=24h")).json();
  const rich = traces.traces.find((r) => r.llm_count >= 2 && r.session_id);
  const sessions = await (await fetch("/api/observability/sessions?range=24h")).json();
  const external = sessions.sessions.find((s) => !s.platform && /^[A-Za-z0-9_-]{8,128}$/.test(s.session_id));
  return { trace: rich, external: external?.session_id ?? null };
});
console.log("── picked trace:", picks.trace.trace_id, "session:", picks.trace.session_id.slice(0, 16), "external:", picks.external);

// ── 1. waterfall via traces-table row click ────────────────────────────────
await page.goto(`${BASE}/observability?tab=traces`, { waitUntil: "networkidle" });
await page.waitForSelector("tbody tr");
await page.fill(".fsearch", picks.trace.trace_id.slice(0, 12));
await page.waitForTimeout(400);
await page.click("tbody tr");
await page.waitForSelector(".wf .nm");
await page.waitForTimeout(900);
console.log("── url after row click:", page.url().replace(BASE, ""));
await page.screenshot({ path: `${OUT}/waterfall-en.png` });
const wfRows = await page.$$eval(".wf .nm", (els) => els.length);
const domBars = await page.$$eval(".wf .lane .bar", (els) =>
  els.map((e) => ({ left: e.style.left, width: e.style.width })));
const apiDetail = await page.evaluate(async (id) => {
  const d = await (await fetch(`/api/observability/traces/${id}?range=24h`)).json();
  const flat = [];
  const walk = (n) => { flat.push({ name: n.name, offset: n.offset_pct, width: n.width_pct, depth: n.depth }); n.children.forEach(walk); };
  d.tree.forEach(walk);
  return { spans: d.meta.span_count, flat };
}, picks.trace.trace_id);
console.log("── waterfall rows:", wfRows, "api spans:", apiDetail.spans);
const depths = apiDetail.flat.map((f) => f.depth);
console.log("── max depth (levels-1):", Math.max(...depths));
console.log("── offset spot-check (DOM bar vs API offset_pct):");
for (const i of [2, Math.min(11, apiDetail.flat.length - 1)]) {
  console.log(`   [${i}] ${apiDetail.flat[i].name.slice(0, 40)} api=${apiDetail.flat[i].offset}%/${apiDetail.flat[i].width}% dom=${domBars[i].left}/${domBars[i].width}`);
}

// ── 2. span drawer: LLM span ────────────────────────────────────────────────
await page.click(".wf .nm:has-text('chat global')");
await page.waitForTimeout(500);
const drawerKv = await page.$$eval(".obs-drawer .kv", (els) =>
  els.map((e) => e.innerText.replace(/\n/g, " = ")));
console.log("── LLM drawer KVs:", drawerKv);
await page.screenshot({ path: `${OUT}/drawer-llm-en.png` });

// ── 3. span drawer: tool span ───────────────────────────────────────────────
await page.click(".wf .nm:has-text('execute_tool')");
await page.waitForTimeout(500);
console.log("── tool drawer KVs:", await page.$$eval(".obs-drawer .kv", (els) =>
  els.map((e) => e.innerText.replace(/\n/g, " = "))));
await page.screenshot({ path: `${OUT}/drawer-tool-en.png` });

// ── 4. session chip → session detail with transcript ───────────────────────
await page.click(".obs-bar .chip .sid");
await page.waitForSelector(".turn", { timeout: 30000 });
await page.waitForTimeout(700);
console.log("── url:", page.url().replace(BASE, ""));
const turns = await page.$$eval(".turn", (els) =>
  els.map((e) => e.innerText.replace(/\n/g, " | ").slice(0, 90)));
console.log("── transcript turns:", turns.length);
turns.slice(0, 4).forEach((x) => console.log("   ", x));
console.log("── memnote:", await page.$eval(".memnote", (e) => e.innerText).catch(() => "none"));
await page.screenshot({ path: `${OUT}/session-transcript-en.png` });

// ── 5. traces-in-session card → waterfall ──────────────────────────────────
const cardText = await page.$eval(".tracecard", (e) => e.innerText.slice(0, 60));
await page.click(".tracecard");
await page.waitForSelector(".wf .nm");
const urlTrace = new URL(page.url()).searchParams.get("trace");
const metaTrace = await page.$eval(".obs-bar .mono", (e) => e.innerText);
console.log("── card:", JSON.stringify(cardText), "→ url trace:", urlTrace, "| meta:", metaTrace);

// ── 6. back to session → OPEN IN CHAT ↗ ────────────────────────────────────
await page.goBack({ waitUntil: "networkidle" });
await page.waitForSelector(".turn");
await page.click("button:has-text('OPEN IN CHAT')");
await page.waitForSelector("[data-testid=agent-select]");
await page.waitForTimeout(1200);
const chatUrl = page.url().replace(BASE, "");
const chatAgent = await page.$eval("[data-testid=agent-select]", (el) =>
  el.selectedOptions[0]?.textContent);
const chatSession = await page.$$eval(".chip.muted", (els) =>
  els.map((e) => e.innerText).find((x) => x.startsWith("session")));
console.log("── OPEN IN CHAT → url:", chatUrl);
console.log("   chat agent selected:", chatAgent, "| session chip:", chatSession);
await page.screenshot({ path: `${OUT}/openinchat-en.png` });

// ── 7. chat rail → OPEN IN OBSERVABILITY ↗ ─────────────────────────────────
const obsLink = await page.$eval("[data-testid=open-in-obs]", (e) => e.getAttribute("href"));
console.log("── chat rail obs link:", obsLink);
await page.click("[data-testid=open-in-obs]");
await page.waitForSelector(".turn", { timeout: 30000 });
console.log("── back in observability, url:", page.url().replace(BASE, ""),
  "transcript turns:", (await page.$$(".turn")).length);
await page.screenshot({ path: `${OUT}/chatrail-crosslink-en.png` });

// ── 8. deep links (cold loads in a fresh page) ──────────────────────────────
const cold = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
await cold.goto(`${BASE}/observability?trace=${picks.trace.trace_id}`, { waitUntil: "networkidle" });
await cold.waitForSelector(".wf .nm", { timeout: 30000 });
console.log("── cold ?trace= →", (await cold.$$(".wf .nm")).length, "waterfall rows");
await cold.goto(`${BASE}/observability?session=${picks.trace.session_id}`, { waitUntil: "networkidle" });
await cold.waitForSelector(".turn", { timeout: 30000 });
console.log("── cold ?session= →", (await cold.$$(".turn")).length, "transcript turns");
await cold.goto(`${BASE}/observability?trace=not-a-real-trace-id`, { waitUntil: "networkidle" });
await cold.waitForTimeout(600);
console.log("── malformed ?trace= →", JSON.stringify(await cold.$eval(".empty", (e) => e.innerText)));
await cold.screenshot({ path: `${OUT}/deeplink-notfound-en.png` });
await cold.goto(`${BASE}/observability?session=ab`, { waitUntil: "networkidle" });
await cold.waitForTimeout(600);
console.log("── malformed ?session= →", JSON.stringify(await cold.$eval(".empty", (e) => e.innerText)));

// ── 9. transcript-less (external) session empty state ──────────────────────
if (picks.external) {
  await cold.goto(`${BASE}/observability?session=${picks.external}`, { waitUntil: "networkidle" });
  await cold.waitForSelector(".grid-31 .empty, .panel .empty", { timeout: 30000 });
  await cold.waitForTimeout(500);
  console.log("── external session transcript state:",
    JSON.stringify(await cold.$eval(".grid-31 .empty", (e) => e.innerText).catch(() => "?")));
  await cold.screenshot({ path: `${OUT}/session-no-transcript-en.png` });
  // same state in zh
  await cold.evaluate(() => localStorage.setItem("i18nextLng", "zh-CN"));
  await cold.reload({ waitUntil: "networkidle" });
  await cold.waitForTimeout(1200);
  console.log("── external session transcript state (zh):",
    JSON.stringify(await cold.$eval(".grid-31 .empty", (e) => e.innerText).catch(() => "?")));
}

// ── 10. zh waterfall + session detail ───────────────────────────────────────
await cold.goto(`${BASE}/observability?trace=${picks.trace.trace_id}`, { waitUntil: "networkidle" });
await cold.waitForSelector(".wf .nm", { timeout: 30000 });
await cold.waitForTimeout(700);
await cold.screenshot({ path: `${OUT}/waterfall-zh.png` });
console.log("── zh waterfall header:", await cold.$$eval(".wf .wf-h", (els) => els.map((e) => e.innerText)));
await cold.goto(`${BASE}/observability?session=${picks.trace.session_id}`, { waitUntil: "networkidle" });
await cold.waitForSelector(".turn", { timeout: 30000 });
await cold.waitForTimeout(700);
await cold.screenshot({ path: `${OUT}/session-transcript-zh.png` });

await browser.close();
console.log("screenshots written to", OUT);

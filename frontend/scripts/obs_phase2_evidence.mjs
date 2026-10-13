// Observability phase-2 evidence: nav, dashboard tiles/charts vs API, range
// switch + refresh cache behavior, traces filters, sessions table, zh locale,
// error state. Usage: node frontend/scripts/obs_phase2_evidence.mjs [baseUrl]
import { mkdirSync } from "node:fs";
import { chromium } from "/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs";

const BASE = process.argv[2] ?? "http://localhost:5174";
const OUT = "design/screenshots/obs-phase2";
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
// V2 Home starts its guided tour on a first visit: mark it seen so it never covers a shot
await page.addInitScript(() => { try { localStorage.setItem("launchpad_v2_tour_done", "1"); } catch { /* storage blocked: nothing to seed */ } });
const setLang = (lang) => page.evaluate((l) => localStorage.setItem("i18nextLng", l), lang);

const tileTexts = () =>
  page.$$eval(".tiles.five .tile", (tiles) =>
    tiles.map((el) => el.innerText.replace(/\n/g, " | ")),
  );

// ── 1. dashboard (en, default 24H) + curl-vs-UI numbers ────────────────────
await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
await setLang("en");
await page.goto(`${BASE}/observability`, { waitUntil: "networkidle" });
await page.waitForSelector(".tiles.five .tile");
await page.waitForTimeout(900);
await page.screenshot({ path: `${OUT}/dashboard-en.png` });
console.log("── DOM tiles (24H):");
for (const t of await tileTexts()) console.log("   ", t);
const apiTiles = await page.evaluate(async () => {
  const res = await fetch("/api/observability/dashboard?range=24h");
  return (await res.json()).tiles;
});
console.log("── API tiles (24h):", JSON.stringify(apiTiles));
console.log(
  "── nav items:",
  await page.$$eval(".side .nav-item", (els) => els.map((e) => e.innerText.replace("\n", " "))),
);
console.log(
  "── tab bar:",
  await page.$$eval(".obs-tab", (els) => els.map((e) => `${e.innerText}${e.className.includes("active") ? "*" : ""}`)),
  "range:",
  await page.$$eval(".range button", (els) =>
    els.map((e) => `${e.innerText}${e.className.includes("on") ? "*" : ""}`)),
);

// ── 2. range switch re-queries (24H → 1H) ──────────────────────────────────
const before = await tileTexts();
const reqUrls = [];
page.on("request", (req) => {
  if (req.url().includes("/api/observability/")) reqUrls.push(req.url());
});
await page.click(".range button:has-text('1H')");
await page.waitForTimeout(4000);
const after = await tileTexts();
console.log("── range switch: requests:", reqUrls.filter((u) => u.includes("range=1h")));
console.log("   tiles 24H:", before[0], "|", before[4]);
console.log("   tiles  1H:", after[0], "|", after[4]);
await page.screenshot({ path: `${OUT}/dashboard-1h-en.png` });

// ── 3. refresh busts cache (force=true, cachehint resets) ──────────────────
await page.waitForTimeout(2500);
const hintBefore = await page.$eval(".cachehint", (e) => e.innerText);
reqUrls.length = 0;
await page.click(".refresh");
await page.waitForTimeout(5000);
const hintAfter = await page.$eval(".cachehint", (e) => e.innerText);
console.log("── refresh: hint before:", JSON.stringify(hintBefore), "→ after:",
  JSON.stringify(hintAfter));
console.log("   force request:", reqUrls.filter((u) => u.includes("force=true")));

// ── 4. traces tab + filters ─────────────────────────────────────────────────
await page.click(".obs-tab:has-text('TRACES')");
await page.waitForSelector("tbody tr", { timeout: 30000 });
await page.waitForTimeout(600);
console.log("── traces rows (unfiltered):", (await page.$$("tbody tr")).length);
await page.selectOption(".filters .fsel", { label: "hr-assistant" });
await page.click(".filters button:has-text('OK')");
await page.waitForTimeout(500);
const filteredRows = await page.$$eval("tbody tr", (rows) =>
  rows.slice(0, 4).map((r) => r.innerText.replace(/\t/g, " · ").replace(/\n/g, " ")),
);
console.log("── traces rows (agent=hr-assistant + status=ok):", filteredRows.length);
for (const r of filteredRows) console.log("   ", r);
await page.screenshot({ path: `${OUT}/traces-filtered-en.png` });

// session search
const sid = await page.$eval("tbody tr .sid", (e) => e.title);
await page.fill(".fsearch", sid.slice(0, 12));
await page.waitForTimeout(400);
console.log("── session search", JSON.stringify(sid.slice(0, 12)), "→ rows:",
  (await page.$$("tbody tr")).length);
await page.screenshot({ path: `${OUT}/traces-search-en.png` });

// ── 5. sessions tab ─────────────────────────────────────────────────────────
await page.click(".obs-tab:has-text('SESSIONS')");
await page.waitForSelector("tbody tr", { timeout: 30000 });
await page.waitForTimeout(600);
const sessRows = await page.$$eval("tbody tr", (rows) =>
  rows.slice(0, 4).map((r) => r.innerText.replace(/\t/g, " · ").replace(/\n/g, " ")),
);
console.log("── sessions rows:", (await page.$$("tbody tr")).length);
for (const r of sessRows) console.log("   ", r);
await page.screenshot({ path: `${OUT}/sessions-en.png` });

// ── 6. zh locale: nav + dashboard + sessions ───────────────────────────────
await setLang("zh-CN");
await page.goto(`${BASE}/observability`, { waitUntil: "networkidle" });
await page.waitForSelector(".tiles.five .tile");
await page.waitForTimeout(900);
console.log(
  "── zh nav:",
  await page.$$eval(".side .nav-item", (els) => els.map((e) => e.innerText.replace("\n", " "))),
);
console.log("── zh tabs:", await page.$$eval(".obs-tab", (els) => els.map((e) => e.innerText)));
await page.screenshot({ path: `${OUT}/dashboard-zh.png` });
await page.click(".obs-tab >> nth=1"); // sessions
await page.waitForSelector("tbody tr", { timeout: 30000 });
await page.waitForTimeout(500);
await page.screenshot({ path: `${OUT}/sessions-zh.png` });

// ── 7. error state: fail the API → toast + retry panel ─────────────────────
await setLang("en");
await page.route("**/api/observability/dashboard*", (route) =>
  route.fulfill({
    status: 502,
    contentType: "application/json",
    body: JSON.stringify({
      code: "observability.query_failed",
      message: "simulated Logs Insights failure",
    }),
  }),
);
await page.goto(`${BASE}/observability`, { waitUntil: "domcontentloaded" });
await page.waitForTimeout(1500);
const hasToast = Boolean(await page.$(".toast.crit"));
const hasRetry = Boolean(await page.$(".obs-error button"));
console.log("── error state: toast:", hasToast, "retry button:", hasRetry);
await page.screenshot({ path: `${OUT}/state-error-en.png` });
await page.unroute("**/api/observability/dashboard*");

await browser.close();
console.log("screenshots written to", OUT);

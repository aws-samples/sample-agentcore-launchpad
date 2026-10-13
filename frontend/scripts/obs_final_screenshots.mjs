// Phase-4 evidence: Observability views × en/zh → design/screenshots/final/obs-*.png
// Usage: node frontend/scripts/obs_final_screenshots.mjs [baseUrl]
import { mkdirSync } from "node:fs";
import { chromium } from "/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs";

const BASE = process.argv[2] ?? "http://localhost:5174";
const OUT = "design/screenshots/final";
mkdirSync(OUT, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
// V2 Home starts its guided tour on a first visit: mark it seen so it never covers a shot
await page.addInitScript(() => { try { localStorage.setItem("launchpad_v2_tour_done", "1"); } catch { /* storage blocked: nothing to seed */ } });
await page.goto(`${BASE}/`, { waitUntil: "networkidle" });

// pick a rich platform trace/session from live data
const pick = await page.evaluate(async () => {
  const traces = await (await fetch("/api/observability/traces?range=24h")).json();
  return traces.traces.find((r) => r.llm_count >= 2 && r.session_id);
});
console.log("picked:", pick.trace_id, pick.session_id.slice(0, 16));

for (const lang of ["en", "zh-CN"]) {
  const tag = lang === "en" ? "en" : "zh";
  await page.evaluate((l) => localStorage.setItem("i18nextLng", l), lang);

  await page.goto(`${BASE}/observability`, { waitUntil: "networkidle" });
  await page.waitForSelector(".tiles.five .tile");
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${OUT}/obs-dashboard-${tag}.png` });

  await page.goto(`${BASE}/observability?tab=traces`, { waitUntil: "networkidle" });
  await page.waitForSelector("tbody tr", { timeout: 30000 });
  await page.waitForTimeout(700);
  await page.screenshot({ path: `${OUT}/obs-traces-${tag}.png` });

  await page.goto(`${BASE}/observability?session=${pick.session_id}`, { waitUntil: "networkidle" });
  await page.waitForSelector(".turn", { timeout: 30000 });
  await page.waitForTimeout(700);
  await page.$eval(".grid-31", (el) => el.scrollIntoView({ block: "start" }));
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${OUT}/obs-sessions-${tag}.png` });

  await page.goto(`${BASE}/observability?trace=${pick.trace_id}`, { waitUntil: "networkidle" });
  await page.waitForSelector(".wf .nm", { timeout: 30000 });
  await page.click(".wf .nm:has-text('chat global')");
  await page.waitForTimeout(700);
  await page.screenshot({ path: `${OUT}/obs-waterfall-${tag}.png` });
  console.log(`${tag}: 4 screenshots done`);
}

await browser.close();
console.log("final screenshots written to", OUT);

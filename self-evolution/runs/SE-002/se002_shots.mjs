import { chromium } from "/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs";
const OUT = "/home/ubuntu/workspace/agentcore_launchpad-worktrees/evo-se-002/.claude/self-evolution/runs/SE-002";
const DEAD = "http://localhost:5198", LIVE = "http://localhost:5197";
const FORBIDDEN = ["none yet", "NO RECORDS", "No knowledge bases yet", "no active agents", "NO EXPERIMENTS", "NO RUNS YET", "NO DEPLOYMENTS YET"];
const browser = await chromium.launch({ args: ["--no-sandbox"] });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await ctx.addInitScript(() => localStorage.setItem("i18nextLng", "en"));
const page = await ctx.newPage();
const report = [];
async function shot(base, path, name, { expectDown }) {
  await page.goto(base + path, { waitUntil: "networkidle" });
  await page.waitForSelector('[data-testid="topbar-health"]');
  await page.waitForTimeout(800);
  const chip = page.locator('[data-testid="topbar-health"]');
  const chipStatus = await chip.getAttribute("data-status");
  const chipText = (await chip.innerText()).trim();
  const body = await page.locator("main").innerText();
  const errors = await page.locator('[data-testid$="load-error"], .load-error').count();
  const retries = await page.locator('.load-error .btn').count();
  const forbidden = FORBIDDEN.filter((f) => body.toLowerCase().includes(f.toLowerCase()));
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false });
  const ok = expectDown ? chipStatus === "down" && errors > 0 && retries > 0 && forbidden.length === 0 : chipStatus === "ok" && errors === 0;
  report.push({ name, chipStatus, chipText, errors, retries, forbidden, ok });
}
await shot(DEAD, "/", "dead_overview", { expectDown: true });
await shot(DEAD, "/registry", "dead_registry", { expectDown: true });
await shot(DEAD, "/knowledge-bases", "dead_knowledge_bases", { expectDown: true });
await shot(DEAD, "/chat", "dead_chat", { expectDown: true });
await shot(DEAD, "/evaluation", "dead_evaluation", { expectDown: true });
await shot(DEAD, "/evaluation?view=experiment", "dead_experiments", { expectDown: true });
await shot(LIVE, "/", "live_overview", { expectDown: false });

// Retry re-issues the fetch: count /api/registry/records requests around a click.
await page.goto(DEAD + "/registry", { waitUntil: "networkidle" });
await page.waitForSelector('[data-testid="registry-load-error"]');
let hits = 0;
page.on("request", (r) => { if (r.url().includes("/api/registry/records")) hits++; });
await page.click('[data-testid="registry-load-error-retry"]');
await page.waitForTimeout(1000);
report.push({ name: "retry_reissues_fetch", requestsAfterClick: hits, ok: hits >= 1 });
console.table(report);
await browser.close();

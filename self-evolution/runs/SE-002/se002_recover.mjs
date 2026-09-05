import { spawn } from "node:child_process";
import { chromium } from "/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs";
const OUT = "/home/ubuntu/workspace/agentcore_launchpad-worktrees/evo-se-002/.claude/self-evolution/runs/SE-002";
const DEAD = "http://localhost:5198";
const browser = await chromium.launch({ args: ["--no-sandbox"] });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
await ctx.addInitScript(() => localStorage.setItem("i18nextLng", "en"));
const page = await ctx.newPage();
await page.goto(DEAD + "/", { waitUntil: "networkidle" });
await page.waitForSelector('[data-testid="topbar-health"][data-status="down"]');
const before = (await page.locator('[data-testid="topbar-health"]').innerText()).trim();
const feedErrBefore = await page.locator('.load-error').count();
// bring a stub backend up on :8999 (what the 5198 vite proxies to) — no reload
const stub = spawn("python3", ["-m", "http.server", "8999", "--bind", "127.0.0.1"], { cwd: "/tmp/se002_stub", stdio: "ignore" });
await new Promise((r) => setTimeout(r, 1200));
// simulate the tab regaining focus → immediate health re-probe
await page.evaluate(() => window.dispatchEvent(new Event("focus")));
await page.waitForSelector('[data-testid="topbar-health"][data-status="ok"]', { timeout: 10000 });
const after = (await page.locator('[data-testid="topbar-health"]').innerText()).trim();
// launch feed: click RETRY → /api/agents now answers {"agents":[]} → the real empty copy
await page.locator('.load-error .btn').first().click();
await page.waitForSelector('text=NO DEPLOYMENTS YET', { timeout: 10000 });
await page.waitForTimeout(500);
await page.screenshot({ path: `${OUT}/recovered_overview.png` });
const feedErrAfter = await page.locator('[data-testid="load-error"]').count(); // health panel still fails (no /api/overview stub) → 1
const body = await page.locator("main").innerText();
console.log(JSON.stringify({ before, after, feedErrBefore, feedErrAfter, showsEmptyFeedCopy: body.includes("NO DEPLOYMENTS YET"), noReload: true }, null, 1));
stub.kill();
await browser.close();

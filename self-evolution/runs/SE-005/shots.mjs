import { chromium } from "/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs";
const RUN = "/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-005";
const BASE = "http://localhost:5197";
const targets = [
  ["kb-detail", "/knowledge-bases?view=detail&kb=does-not-exist", "section"],
  ["registry-edit", "/registry?view=edit&record=does-not-exist", "section"],
  ["memory-short-term", "/memory?view=short-term&actor=does-not-exist", "body"],
];
const browser = await chromium.launch({ args: ["--no-sandbox"] });
const out = {};
for (const lng of ["en", "zh-CN"]) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  await ctx.addInitScript((l) => localStorage.setItem("i18nextLng", l), lng);
  const page = await ctx.newPage();
  for (const [name, path] of targets) {
    await page.goto(BASE + path, { waitUntil: "networkidle" });
    await page.waitForTimeout(1500);
    const text = await page.evaluate(() => document.body.innerText);
    const lines = text.split("\n").filter((l) => /AWS|denied|not found|invalid|rejected|拒绝|未找到|无效|Failed to load|COULD NOT|lookup failed|查找失败|无法/i.test(l));
    out[`${lng}/${name}`] = lines.slice(0, 6);
    await page.screenshot({ path: `${RUN}/${name}_${lng}.png`, fullPage: false });
  }
  await ctx.close();
}
await browser.close();
console.log(JSON.stringify(out, null, 2));

import { chromium } from "/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs";
const BASE = "http://localhost:5197", API = "http://localhost:8000";
const OUT = "/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-007";
const j = async (u) => (await fetch(API + u)).json();
const [ds, ev, oe, ex, ag, kb] = await Promise.all([
  j("/api/eval/datasets"), j("/api/eval/evaluators"), j("/api/eval/online"),
  j("/api/experiments"), j("/api/agents"), j("/api/knowledge-bases"),
]);
const valid = {
  ds: ds.datasets[0]?.id, ev: (ev.evaluators.find((e) => e.source === "custom") ?? ev.evaluators[0])?.id,
  oe: oe.configs[0]?.config_id, exp: ex.experiments[0]?.id,
  agent: ag.agents.find((a) => a.status === "active" && a.invoke_capability?.eligible)?.id,
  kb: kb.items[0]?.kb_id,
};
const STALE = [
  ["datasets", "/evaluation?view=datasets&ds=does-not-exist", "ds"],
  ["evaluators", "/evaluation?view=evaluators&ev=does-not-exist", "ev"],
  ["online", "/evaluation?view=online&oe=does-not-exist", "oe"],
  ["experiment", "/evaluation?view=experiment&exp=does-not-exist", "exp"],
  ["chat", "/chat?agent=does-not-exist&session=does-not-exist-either", "agent"],
  ["kb", "/knowledge-bases?view=detail&kb=does-not-exist", "kb"],
  ["kb-missing", "/knowledge-bases?view=detail", "kb"],
];
const lines = [];
const log = (s) => { lines.push(s); console.log(s); };
let fails = 0;
const check = (ok, msg) => { log(`${ok ? "PASS" : "FAIL"} ${msg}`); if (!ok) fails++; };
const browser = await chromium.launch({ args: ["--no-sandbox"] });
for (const lng of ["en", "zh-CN"]) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await ctx.addInitScript((l) => localStorage.setItem("i18nextLng", l), lng);
  const page = await ctx.newPage();
  for (const [name, url, param] of STALE) {
    await page.goto(BASE + url);
    const notice = page.locator("[data-testid=stale-link]");
    await notice.waitFor({ timeout: 15000 }).catch(() => {});
    const visible = await notice.isVisible().catch(() => false);
    const text = visible ? (await notice.innerText()).replace(/\s+/g, " ").trim() : "(none)";
    const search = await page.evaluate(() => location.search);
    log(`[${lng}] ${name} ${url}`);
    log(`   notice: ${text}`);
    log(`   location.search after render: ${JSON.stringify(search)}`);
    check(visible, `${lng}/${name}: notice rendered`);
    check(!new URLSearchParams(search).has(param), `${lng}/${name}: stale param '${param}' removed`);
    if (name === "chat") {
      await page.waitForTimeout(500);
      const val = await page.locator("select[data-testid=agent-select]").inputValue();
      log(`   agent-select value: ${JSON.stringify(val)}`);
      check(val === "", `${lng}/chat: picker stays on placeholder`);
      check(!new URLSearchParams(search).has("session"), `${lng}/chat: stale session param removed`);
    }
    if (name === "kb-missing") {
      // the list's own transient loading row may still be up — it must clear
      await page.locator(".loading-line").first().waitFor({ state: "detached", timeout: 15000 }).catch(() => {});
      const loading = await page.locator(".loading-line").count();
      check(loading === 0, `${lng}/kb-missing: no permanent LOADING line (count=${loading})`);
    }
    if (lng === "en") await page.screenshot({ path: `${OUT}/stale-${name}.png`, fullPage: false });
    if (lng === "zh-CN" && name === "datasets") await page.screenshot({ path: `${OUT}/stale-${name}-zh.png` });
    if (name === "datasets" && lng === "en") {
      await page.locator("[data-testid=stale-link-dismiss]").click();
      check((await notice.count()) === 0, `en/datasets: notice dismissible`);
    }
  }
  await ctx.close();
}
// ---- valid deep links: no notice, param kept, row/agent selected
const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
const page = await ctx.newPage();
const VALID = [
  ["datasets", `/evaluation?view=datasets&ds=${valid.ds}`, "ds", valid.ds],
  ["evaluators", `/evaluation?view=evaluators&ev=${valid.ev}`, "ev", valid.ev],
  ["online", `/evaluation?view=online&oe=${valid.oe}`, "oe", valid.oe],
  ["experiment", `/evaluation?view=experiment&exp=${valid.exp}`, "exp", valid.exp],
  ["chat", `/chat?agent=${valid.agent}`, "agent", valid.agent],
  ["kb", `/knowledge-bases?view=detail&kb=${valid.kb}`, "kb", valid.kb],
];
for (const [name, url, param, id] of VALID) {
  if (!id) { log(`SKIP valid/${name}: no ${param} in the workspace`); continue; }
  await page.goto(BASE + url);
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(1500);
  const search = await page.evaluate(() => location.search);
  const notice = await page.locator("[data-testid=stale-link]").count();
  log(`valid/${name} ${url} → search=${JSON.stringify(search)} notice=${notice}`);
  check(notice === 0, `valid/${name}: no notice`);
  check(new URLSearchParams(search).get(param) === id, `valid/${name}: '${param}=${id}' kept`);
  if (name === "chat") {
    const val = await page.locator("select[data-testid=agent-select]").inputValue();
    check(val === id, `valid/chat: agent-select value === ${id}`);
  } else if (name === "kb") {
    const h1 = await page.locator("h1").first().innerText();
    check(h1.trim() === kb.items[0].name, `valid/kb: detail h1 '${h1.trim()}' === '${kb.items[0].name}'`);
  } else {
    // the selected row is the one with the amber highlight (tr.sel or inline bg)
    const rowText = await page.evaluate(() => {
      const lit = (el) => getComputedStyle(el).backgroundColor !== "rgba(0, 0, 0, 0)";
      const tr = [...document.querySelectorAll("tbody tr")].find((r) => lit(r) || [...r.children].some(lit));
      return tr ? tr.innerText.replace(/\s+/g, " ").slice(0, 140) : null;
    });
    const label = { datasets: ds.datasets[0]?.name, evaluators: (ev.evaluators.find((e) => e.id === valid.ev)?.name ?? valid.ev.split("-")[0]), online: oe.configs[0]?.name, experiment: ex.experiments[0]?.name }[name];
    check(rowText !== null && rowText.includes(label), `valid/${name}: highlighted row names '${label}' → ${rowText}`);
  }
}
await ctx.close();
await browser.close();
log(`\nRESULT: ${fails === 0 ? "ALL PASS" : fails + " FAIL"}`);
const fs = await import("node:fs");
fs.writeFileSync(`${OUT}/stale_links.txt`, lines.join("\n") + "\n");
process.exit(fails ? 1 : 0);

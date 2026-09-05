import { chromium } from "/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs";
import fs from "node:fs";
const OUT = "/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-006";
const BASE = "http://localhost:5197";
const rec = await fetch("http://localhost:8000/api/registry/records").then(r => r.json());
const recordId = rec.records[0].record_id;
const PAGES = [
  ["register", `/registry?view=register`],
  ["edit", `/registry?view=edit&record=${recordId}`],
  ["kb_create", `/knowledge-bases?view=create`],
  ["studio", `/create/studio`],
  ["online_eval", `/evaluation?view=online&oe=new`],
  ["workspace_detail", `/workspaces?view=detail&ws=lab-use2`],
];
const lines = [];
const log = (s) => { lines.push(s); console.log(s); };
const browser = await chromium.launch({ args: ["--no-sandbox"] });
let failures = 0;
const collect = () => [...document.querySelectorAll(".btn.primary:disabled")].map(b => {
  const id = b.getAttribute("aria-describedby");
  const hint = id ? document.getElementById(id) : null;
  const vis = hint ? (() => { const r = hint.getBoundingClientRect(); const cs = getComputedStyle(hint); return r.width > 0 && r.height > 0 && cs.visibility !== "hidden" && cs.display !== "none"; })() : false;
  return { txt: b.textContent.trim(), title: b.title, describedby: id, hintText: hint ? hint.textContent.trim() : null, hintVisible: vis, hintFont: hint ? getComputedStyle(hint).fontFamily.slice(0, 30) : null };
});
for (const locale of ["en", "zh-CN"]) {
  const ctx = await browser.newContext({ viewport: { width: 1360, height: 900 } });
  await ctx.addInitScript((l) => localStorage.setItem("i18nextLng", l), locale);
  const page = await ctx.newPage();
  for (const [name, path] of PAGES) {
    await page.goto(BASE + path, { waitUntil: "networkidle" });
    await page.waitForTimeout(800);
    const rows = await page.evaluate(collect);
    log(`\n[${locale}] ${name} ${path}`);
    if (rows.length === 0) { log("  !! no disabled .btn.primary found"); failures++; }
    for (const r of rows) {
      const ok = r.title.length > 0 && r.describedby && r.hintVisible && r.hintText === r.title;
      if (!ok) failures++;
      log(`  ${ok ? "OK " : "BAD"} btn="${r.txt}" title="${r.title}" describedby=${r.describedby} hintVisible=${r.hintVisible} hintText="${r.hintText}"`);
    }
    if (locale === "en" && (name === "register" || name === "kb_create")) {
      await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false });
    }
    if (name === "register") {
      await page.fill("#reg-name", "probe-mcp-server");
      await page.fill("#reg-url", "https://example.com/mcp");
      await page.waitForTimeout(200);
      const after = await page.evaluate(() => {
        const b = [...document.querySelectorAll(".btn.primary")].find(x => x.textContent.includes("REGISTER") || x.textContent.includes("注册"));
        return { disabled: b.disabled, title: b.title, describedby: b.getAttribute("aria-describedby"), hints: document.querySelectorAll(".btn-hint").length };
      });
      const ok = !after.disabled && after.title === "" && !after.describedby && after.hints === 0;
      if (!ok) failures++;
      log(`  ${ok ? "OK " : "BAD"} after valid name+URL: disabled=${after.disabled} title="${after.title}" describedby=${after.describedby} hints=${after.hints}`);
      if (locale === "en") await page.screenshot({ path: `${OUT}/register_valid.png` });
    }
  }
  await ctx.close();
}
await browser.close();
log(`\nRESULT: ${failures === 0 ? "PASS" : "FAIL"} (${failures} failing checks)`);
fs.writeFileSync(`${OUT}/hints.txt`, lines.join("\n") + "\n");
process.exit(failures === 0 ? 0 : 1);

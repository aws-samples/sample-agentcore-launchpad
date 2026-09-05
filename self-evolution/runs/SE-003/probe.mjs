import { chromium } from "/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs";
const BASE = process.env.BASE || "http://localhost:5197";
const LNG = process.env.LNG || "en";
const ROUTES = ["/", "/create", "/create/studio", "/registry", "/registry?view=register", "/registry?view=a2a-demo",
  "/knowledge-bases", "/knowledge-bases?view=create", "/memory", "/memory?view=short-term", "/memory?view=long-term",
  "/chat", "/observability", "/observability?tab=sessions", "/observability?tab=traces", "/evaluation",
  "/evaluation?view=experiment", "/evaluation?view=evaluators", "/evaluation?view=datasets", "/evaluation?view=online",
  "/skill-lab", "/skill-lab?view=eval", "/skill-lab?view=train", "/governance", "/governance?view=policy",
  "/governance?view=decisions", "/governance?view=audit", "/users", "/workspaces", "/workspaces?view=create"];
const PROBE = () => [...document.querySelectorAll('input,select,textarea')]
  .filter(i => i.type !== 'hidden' && i.getBoundingClientRect().width > 0)
  .filter(i => !(i.id && document.querySelector(`label[for="${i.id}"]`)) && !i.closest('label')
    && !i.getAttribute('aria-label') && !i.getAttribute('aria-labelledby') && !i.getAttribute('placeholder'))
  .map(i => i.outerHTML.slice(0, 100));
const browser = await chromium.launch({ args: ["--no-sandbox"] });
const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
await ctx.addInitScript((lng) => localStorage.setItem("i18nextLng", lng), LNG);
const page = await ctx.newPage();
let total = 0;
console.log(`# a11y probe · base=${BASE} · lng=${LNG} · ${new Date().toISOString()}`);
for (const r of ROUTES) {
  try {
    await page.goto(BASE + r, { waitUntil: "networkidle", timeout: 30000 });
  } catch (e) { console.log(`${r}\tNAV-ERROR ${e.message.split("\n")[0]}`); continue; }
  await page.waitForTimeout(800);
  const hits = await page.evaluate(PROBE);
  total += hits.length;
  console.log(`${r}\t${JSON.stringify(hits)}`);
}
console.log(`# total unlabeled controls: ${total}`);
await browser.close();
process.exit(total === 0 ? 0 : 2);

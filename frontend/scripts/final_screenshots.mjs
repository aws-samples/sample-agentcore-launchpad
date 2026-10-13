// Phase 13 evidence: every view in both locales + UI-state demonstrations.
// Usage: node frontend/scripts/final_screenshots.mjs [baseUrl]
import { mkdirSync } from "node:fs";
import { chromium } from "/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs";

const BASE = process.argv[2] ?? "http://localhost:5174";
const OUT = "design/screenshots/final";
mkdirSync(OUT, { recursive: true });

const VIEWS = [
  ["overview", "/"],
  ["create-method", "/create"],
  ["registry", "/registry"],
  ["chat", "/chat"],
  ["evaluation", "/evaluation"],
  ["governance", "/governance"],
];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
// V2 Home starts its guided tour on a first visit: mark it seen so it never covers a shot
await page.addInitScript(() => { try { localStorage.setItem("launchpad_v2_tour_done", "1"); } catch { /* storage blocked: nothing to seed */ } });

async function setLang(lang) {
  await page.evaluate((l) => localStorage.setItem("i18nextLng", l), lang);
}

for (const lang of ["en", "zh-CN"]) {
  await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
  await setLang(lang);
  const tag = lang === "en" ? "en" : "zh";
  for (const [name, path] of VIEWS) {
    await page.goto(`${BASE}${path}`, { waitUntil: "networkidle" });
    await page.waitForTimeout(1400);
    await page.screenshot({ path: `${OUT}/${name}-${tag}.png` });
  }
  // wizard step 2 (configure)
  await page.goto(`${BASE}/create`, { waitUntil: "networkidle" });
  await page.waitForTimeout(600);
  await page.click(".methods .method");
  await page.click("button:has-text('▸')");
  await page.waitForTimeout(800);
  await page.screenshot({ path: `${OUT}/create-configure-${tag}.png` });
}

// ── UI-state demonstrations (en) ──────────────────────────────────────────
await page.goto(`${BASE}/`, { waitUntil: "networkidle" });
await setLang("en");

// 1. loading state: hold the registry API for 8s and catch the loading line
await page.route("**/api/registry/records", async (route) => {
  await new Promise((r) => setTimeout(r, 8000));
  await route.continue();
});
await page.goto(`${BASE}/registry`, { waitUntil: "domcontentloaded" });
await page.waitForTimeout(1200);
await page.screenshot({ path: `${OUT}/state-loading-registry.png` });
await page.unroute("**/api/registry/records");

// 2. confirm dialog: select an APPROVED record and click disable
await page.goto(`${BASE}/registry`, { waitUntil: "networkidle" });
await page.waitForTimeout(1500);
const rows = await page.$$("tbody tr");
let dialogShot = false;
for (const row of rows) {
  await row.click();
  await page.waitForTimeout(700);
  const disableBtn = await page.$(".drawer button:has-text('Disable')");
  if (disableBtn) {
    await disableBtn.click();
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${OUT}/state-confirm-dialog.png` });
    await page.click(".confirm-actions button:has-text('Cancel')");
    dialogShot = true;
    break;
  }
}
console.log("confirm dialog demonstrated:", dialogShot);

// 3. error toast: make the action endpoint fail, bypass confirm via approve
await page.route("**/api/registry/records/*/action", (route) =>
  route.fulfill({
    status: 500,
    contentType: "application/json",
    body: JSON.stringify({ code: "registry.error", message: "simulated backend failure" }),
  }),
);
let toastShot = false;
for (const row of await page.$$("tbody tr")) {
  await row.click();
  await page.waitForTimeout(600);
  const actionBtn = await page.$(
    ".drawer button:has-text('Submit'), .drawer button:has-text('Approve')",
  );
  if (actionBtn) {
    await actionBtn.click();
    await page.waitForTimeout(900);
    await page.screenshot({ path: `${OUT}/state-error-toast.png` });
    toastShot = await page.$(".toast.crit").then(Boolean);
    break;
  }
}
console.log("error toast demonstrated:", toastShot);

// 4. empty state: chat thread before the first message
await page.goto(`${BASE}/chat`, { waitUntil: "networkidle" });
await page.waitForTimeout(1200);
await page.screenshot({ path: `${OUT}/state-empty-chat.png` });

await browser.close();
console.log("screenshots written to", OUT);

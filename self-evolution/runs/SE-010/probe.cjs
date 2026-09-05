const { chromium } = require("/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright");
const fs = require("fs");
const RUNS = __dirname;
const CSS = "/home/ubuntu/workspace/agentcore_launchpad-worktrees/evo-se-010/frontend/src/theme/app.css";
const bundle = fs.readFileSync(`${RUNS}/probe-bundle.js`, "utf8");

const READY = {
  kind: "runtime", resource_id: "hr_assistant-AbCdEf1234",
  versions: [
    { version: "3", status: "READY", description: "v3", last_updated_at: "2026-09-01T12:03:00+00:00" },
    { version: "2", status: "READY", description: "v2", last_updated_at: "2026-09-01T12:02:00+00:00" },
    { version: "1", status: "READY", description: null, last_updated_at: "2026-09-01T12:01:00+00:00" },
  ],
  endpoints: [
    { name: "DEFAULT", live_version: "3", target_version: "3", status: "READY", description: null, created_at: "2026-08-01T00:00:00+00:00", last_updated_at: "2026-09-01T00:00:00+00:00", failure_reason: null },
    { name: "stable", live_version: "2", target_version: "3", status: "UPDATE_FAILED", description: "canary stable", created_at: "2026-08-01T00:00:00+00:00", last_updated_at: "2026-09-01T00:00:00+00:00", failure_reason: "boom" },
    { name: "treatment", live_version: "3", target_version: "3", status: "CREATING", description: null, created_at: "2026-08-01T00:00:00+00:00", last_updated_at: "2026-09-01T00:00:00+00:00", failure_reason: null },
  ],
  latest_version: "3", ledger_version: "2", canary_endpoints: ["stable", "treatment"],
};
const EMPTY = { kind: "harness", resource_id: "hr-harness-XyZ987", versions: [], endpoints: [], latest_version: null, ledger_version: "1", canary_endpoints: [] };
const SYNC = { ...READY, kind: "harness", ledger_version: "3", endpoints: [READY.endpoints[0]], canary_endpoints: [] };

const scenarios = {
  ready: { status: 200, body: READY, delay: 400 },
  sync: { status: 200, body: SYNC },
  empty: { status: 200, body: EMPTY },
  no_resource: { status: 409, body: { code: "agent.no_resource", message: "The agent has no AWS resource yet (status: deploying) — versions appear once the deploy stage has created it.", detail: null } },
  error: { status: 502, body: { code: "aws.unavailable", message: "AWS is throttling this request", detail: { aws_error_code: "ThrottlingException" } } },
};

(async () => {
  const browser = await chromium.launch({ args: ["--no-sandbox"] });
  const results = {};
  for (const lang of ["en", "zh-CN"]) {
    for (const [name, sc] of Object.entries(scenarios)) {
      const page = await browser.newPage({ viewport: { width: 1000, height: 900 } });
      await page.addInitScript((l) => localStorage.setItem("i18nextLng", l), lang);
      await page.route("**/api/agents/a1/versions", async (route) => {
        if (sc.delay) await new Promise((r) => setTimeout(r, sc.delay));
        await route.fulfill({ status: sc.status, contentType: "application/json", body: JSON.stringify(sc.body) });
      });
      await page.route("http://probe.local/", (route) =>
        route.fulfill({ status: 200, contentType: "text/html", body: `<!doctype html><html><head><style>${fs.readFileSync(CSS, "utf8")}</style></head><body style="padding:16px"><div id="root"></div></body></html>` }));
      await page.goto("http://probe.local/");
      await page.addScriptTag({ content: bundle });
      const r = { lang, scenario: name };
      if (sc.delay) {
        r.loadingSeen = (await page.locator('[data-testid="versions-loading"]').count()) === 1;
        r.loadingText = await page.locator('[data-testid="versions-loading"]').textContent();
      }
      const expectPhase = sc.status === 200 ? "ready" : sc.status === 409 ? "no_resource" : "error";
      await page.waitForSelector(`[data-testid="versions-panel"][data-phase="${expectPhase}"]`, { timeout: 8000 });
      r.phase = expectPhase;
      r.title = await page.locator(".phead h2").first().textContent();
      if (expectPhase === "ready") {
        r.defaultRows = await page.locator('[data-testid="endpoint-row"][data-default="true"]').count();
        r.defaultTag = r.defaultRows ? await page.locator('[data-testid="endpoint-row"][data-default="true"] .chip').first().textContent() : null;
        r.canaryRows = await page.locator('[data-testid="endpoint-row"][data-canary="true"]').count();
        r.mismatchChip = await page.locator('[data-testid="versions-mismatch"]').count();
        r.canaryNote = await page.locator('[data-testid="versions-canary-note"]').count();
        r.ledgerRows = await page.locator('[data-testid="version-row"][data-ledger="true"]').count();
        r.versionsEmpty = await page.locator('[data-testid="versions-empty"]').count();
        r.endpointsEmpty = await page.locator('[data-testid="endpoints-empty"]').count();
        r.summary = await page.locator('[data-testid="versions-summary"]').textContent();
      } else if (expectPhase === "no_resource") {
        r.text = await page.locator('[data-testid="versions-no-resource"]').textContent();
      } else {
        r.text = await page.locator('[data-testid="versions-error"]').textContent();
        r.retry = await page.locator('[data-testid="versions-error-retry"]').count();
      }
      await page.screenshot({ path: `${RUNS}/panel-${lang}-${name}.png`, fullPage: true });
      results[`${lang}/${name}`] = r;
      await page.close();
    }
  }
  await browser.close();
  fs.writeFileSync(`${RUNS}/probe-results.json`, JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results, null, 2));
})().catch((e) => { console.error(e); process.exit(1); });

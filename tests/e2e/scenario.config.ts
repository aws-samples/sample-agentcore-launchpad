import { defineConfig, devices } from "@playwright/test";

/**
 * END-TO-END SCENARIO suite — one business storyline (the "Octank HR assistant") that chains
 * every Launchpad-CN module: knowledge → tools → skills → build → use → observe → evaluate →
 * improve → go live → govern (D20 Policy, s11) → model providers (D21, s12) → lifecycle
 * (`docs/cn/reports/e2e-scenario-plan.md`).
 *
 * A separate config (not `playwright.config.ts`) so the storyline gets its own naming
 * prefix, evidence folder, ledger and report directory without touching the N10/N12
 * campaign runs. Like the campaign suite it drives a real, deployed console and creates
 * real AgentCore resources — never part of `make verify`.
 *
 *   cd tests/e2e
 *   export LAUNCHPAD_E2E_BASE_URL=https://<host>:8443
 *   export LAUNCHPAD_E2E_PASSWORD="$(aws ssm get-parameter … --with-decryption …)"   # in-process only
 *   npx playwright test -c scenario.config.ts --list                       # enumerate the steps
 *   npx playwright test -c scenario.config.ts                              # the whole storyline
 *   SCENARIO_RESUME_FROM=s07 npx playwright test -c scenario.config.ts     # resume at a step (s11 and s12 sit between s09 and s10)
 *   npx playwright test -c scenario.config.ts cn/scenario/scenario-cleanup.spec.ts
 */
process.env.N10_PREFIX ??= "e2es-";
process.env.N10_EVIDENCE_ROUND ??= "scenario/all"; // bulk (git-ignored); curated key shots land in docs/cn/evidence/scenario/
process.env.N10_LEDGER ??= "../../../.superpowers/sdd/e2e-scenario/progress.md";

const baseURL = process.env.LAUNCHPAD_E2E_BASE_URL ?? "https://203.0.113.60:8443";

export default defineConfig({
  testDir: ".",
  testMatch: /cn\/(auth\.setup|scenario\/.*\.spec)\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  // steps chain deploys (≈5 min each), worker jobs (Skill Lab / evaluation, 5–20 min) and
  // a canary (≈20 min); each step sets its own ceiling with test.setTimeout
  timeout: 60 * 60 * 1000,
  expect: { timeout: 20_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report-scenario" }]],
  outputDir: "test-results-scenario",
  use: {
    baseURL,
    ignoreHTTPSErrors: true,
    locale: "en-US",
    viewport: { width: 1440, height: 900 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
  },
  projects: [
    { name: "setup", testMatch: /cn\/auth\.setup\.ts/ },
    // Playwright runs a project's files in alphabetical order, and s11 (govern, D20) must run while
    // the HR assistant still exists — i.e. BEFORE s10 (lifecycle deletes it); s12 (model providers,
    // D21) follows s11 for the same reason (it reads the s00 member identity and leaves the agents
    // alone). Two projects in config order give the chain s00 … s09 → s11 → s12 → s10 → cleanup
    // without renaming any file. Neither depends on the other (both only on `setup`), so a failed
    // step never skips the lifecycle / cleanup half the way a `dependencies` edge would.
    // `scn.STEPS` carries the same order.
    {
      name: "scenario",
      testMatch: /cn\/scenario\/(s0\d|s11|s12)-.*\.spec\.ts/,
      dependencies: ["setup"],
      use: { ...devices["Desktop Chrome"], storageState: ".auth/state.json" },
    },
    {
      name: "lifecycle",
      testMatch: /cn\/scenario\/(s10-.*|scenario-cleanup)\.spec\.ts/,
      dependencies: ["setup"],
      use: { ...devices["Desktop Chrome"], storageState: ".auth/state.json" },
    },
  ],
});

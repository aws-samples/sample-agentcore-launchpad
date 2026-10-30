import { defineConfig, devices } from "@playwright/test";

/**
 * TARGETED verification suite — short, focused storylines that re-test one product path on a
 * real environment without the 2 h scenario chain (`docs/cn/reports/e2e-scenario-plan.md` §9).
 * Same conventions as `scenario.config.ts` (prefix `e2es-`, bulk evidence in `scenario/all`,
 * curated shots in `docs/cn/evidence/scenario/`), separate ledger / report folders, and a
 * testMatch that the full scenario chain never picks up.
 *
 *   cd tests/e2e && npx playwright test -c targeted.config.ts cn/targeted/t01-canary-ramp.spec.ts
 */
process.env.N10_PREFIX ??= "e2es-";
process.env.N10_EVIDENCE_ROUND ??= "scenario/all";
process.env.N10_LEDGER ??= "../../../.superpowers/sdd/targeted-canary/progress.md";

const baseURL = process.env.LAUNCHPAD_E2E_BASE_URL ?? "https://203.0.113.70:8443";

export default defineConfig({
  testDir: ".",
  testMatch: /cn\/(auth\.setup|targeted\/.*\.spec)\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60 * 60 * 1000,
  expect: { timeout: 20_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report-targeted" }]],
  outputDir: "test-results-targeted",
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
    {
      name: "targeted",
      testMatch: /cn\/targeted\/.*\.spec\.ts/,
      dependencies: ["setup"],
      use: { ...devices["Desktop Chrome"], storageState: ".auth/state.json" },
    },
  ],
});

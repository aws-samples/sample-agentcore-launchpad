import { defineConfig, devices } from "@playwright/test";

/**
 * Round-16 UI combination suites (tests/e2e/cn/combinations/): wizard pairwise, page
 * pairwise and the seeded UI random walk against a live console. NOT part of `make verify`
 * (the generators / spec tables ARE — `node --test cn/combinations/*.unit.test.ts`).
 *
 *   LAUNCHPAD_E2E_BASE_URL          https://<host>:8443 (REQUIRED — no default host)
 *   LAUNCHPAD_E2E_PASSWORD          admin password, fetched in-process from SSM — never on disk
 *   LAUNCHPAD_E2E_MEMBER_PASSWORD   password the suite gives the x16n-member fixture (any)
 *   X16N_OUT                        evidence dir (default /home/ubuntu/designs/combinations-20261019)
 *   X16N_SEED                       pairwise / random-walk seed (default 16)
 *   X16N_LIVE_CAP                   live wizard submissions per wizard (default 6)
 *
 *   npx playwright test -c combinations.config.ts cn/combinations/wizard-pairwise.spec.ts
 *
 * Each spec signs in through the API on its own browser context (no storageState file).
 */
export default defineConfig({
  testDir: "./cn/combinations",
  testMatch: /.*\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120 * 60 * 1000,
  expect: { timeout: 15_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report-combinations" }]],
  outputDir: "test-results-combinations",
  use: {
    ...devices["Desktop Chrome"],
    baseURL: process.env.LAUNCHPAD_E2E_BASE_URL,
    ignoreHTTPSErrors: true,
    locale: "en-US",
    viewport: { width: 1600, height: 1000 },
    screenshot: "off",
    trace: "off",
    actionTimeout: 20_000,
    navigationTimeout: 60_000,
  },
});

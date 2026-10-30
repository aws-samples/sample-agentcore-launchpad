import { defineConfig, devices } from "@playwright/test";

/**
 * N10 end-to-end suite for the Launchpad-CN console.
 *
 * Deliberately NOT wired into `make verify`: it drives a real, deployed console
 * (self-signed TLS on :8443) and creates real AgentCore resources through the
 * product's own UI/API. Configure via env:
 *   LAUNCHPAD_E2E_BASE_URL   https://<host>:8443 (required)
 *   LAUNCHPAD_E2E_USERNAME   defaults to "admin"
 *   LAUNCHPAD_E2E_PASSWORD   never written to disk; fetch it in-process, e.g.
 *     export LAUNCHPAD_E2E_PASSWORD="$(aws ssm get-parameter --name launchpad-console-password \
 *        --with-decryption --query Parameter.Value --output text --profile cn-northwest-1 --region cn-northwest-1)"
 */
const baseURL = process.env.LAUNCHPAD_E2E_BASE_URL ?? "https://203.0.113.60:8443";

export default defineConfig({
  testDir: ".",
  testMatch: /cn\/.*\.(spec|setup)\.ts/,
  // the e2e SCENARIO storyline has its own config (scenario.config.ts) and must not run with the campaigns
  testIgnore: /cn\/scenario\//,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  // Agent deploys build ARM64 images through CodeBuild (5–15 min each); several
  // cases chain deploy → chat → memory, so the per-test ceiling is generous.
  timeout: 30 * 60 * 1000,
  expect: { timeout: 20_000 },
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  outputDir: "test-results",
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
      name: "cn",
      testMatch: /cn\/.*\.spec\.ts/,
      testIgnore: /cn\/scenario\//,
      dependencies: ["setup"],
      use: { ...devices["Desktop Chrome"], storageState: ".auth/state.json" },
    },
  ],
});

import { test as setup, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { stateGet, stateSet } from "./helpers";

const STATE = path.resolve(__dirname, "../.auth/state.json");

/**
 * Signs in once through the real login form and stores the session cookie as
 * Playwright storageState. The password comes from LAUNCHPAD_E2E_PASSWORD only —
 * it is typed into the masked field and never logged or screenshotted.
 */
setup("login as admin and persist storage state", async ({ page }) => {
  const username = process.env.LAUNCHPAD_E2E_USERNAME ?? "admin";
  const password = process.env.LAUNCHPAD_E2E_PASSWORD;
  if (!password) throw new Error("LAUNCHPAD_E2E_PASSWORD is not set (fetch it in-process from SSM)");

  await page.goto("/");
  await page.locator('input[autocomplete="username"]').fill(username);
  await page.locator('input[type="password"][autocomplete="current-password"]').fill(password);
  await page.locator('input[type="password"][autocomplete="current-password"]').press("Enter");

  // The shell is up once the sidebar nav renders (either console flavour).
  // the V2 shell renders its sidebar as <aside aria-label="Console navigation"> (the top-bar product tab, the last <nav>, went with 262ca426); the classic console keeps <nav>
  await expect(page.locator("nav, aside[aria-label]").first()).toBeVisible({ timeout: 60_000 });
  const status = await page.request.get("/api/auth/status");
  expect(status.ok()).toBeTruthy();
  const body = await status.json();
  expect(body.authenticated).toBe(true);
  expect(body.username).toBe(username);

  fs.mkdirSync(path.dirname(STATE), { recursive: true });
  await page.context().storageState({ path: STATE });
  // round-4: the run start bounds the Skill Lab jobs the cleanup may delete (jobs on the shared sample skill)
  // (two parallel runs — agents / Skill Lab — share the file: the earliest login wins)
  if (!process.env.N10_REUSE && !stateGet("run_start")) stateSet("run_start", new Date().toISOString());
});

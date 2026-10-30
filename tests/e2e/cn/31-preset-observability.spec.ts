import { expect, test } from "@playwright/test";
import { attach, evidence, ledger, loginFresh, registryRecords, stateSet, E2E_PREFIX } from "./helpers";

/**
 * Round-4 observations re-check (read-only on the product, no AWS):
 *  - N12-obs31 (c): every `GET /api/system-agents` entry carries `version`, `release`,
 *    `installed_release` and `last_job`; the installed `eval-worker` has them populated.
 *  - N12-obs31 (a): `GET /api/system-agents/eval-worker/jobs/<last_job.id>` returns the job
 *    + log for an admin and 403 for a member (a throwaway member is registered through the
 *    sign-in page and approved in the Users page, exactly like case 12; deleted in cleanup).
 *  - N11-obs31: `GET /api/registry/records` holds exactly ONE `expense-report-writer` skill
 *    record (bootstrap re-runs no longer add PENDING duplicates). Its status is recorded; the
 *    "already registered" branch keeps the existing status and ruling (a) re-approves only a
 *    record that WAS approved when it is refreshed — so APPROVED is required only when the
 *    record detail shows an earlier approval (`decided_by` / `status_reason`).
 */
test("obs31: preset entries carry version/release/installed_release/last_job; preset job route admin 200 / member 403; one APPROVED expense-report-writer", async ({ page, browser }, testInfo) => {
  const baseURL = test.info().project.use.baseURL as string;
  const body = await (await page.request.get("/api/system-agents")).json();
  const presets = (body.presets ?? []) as Array<Record<string, unknown>>;
  await attach(testInfo, "system-agents.json", body);
  const shape = presets.map((p) => ({ key: p.key, status: p.status, version: p.version, release: p.release, installed_release: p.installed_release, last_job: p.last_job }));
  ledger(`obs31 presets=${JSON.stringify(shape).slice(0, 900)}`);
  for (const p of presets) {
    for (const k of ["version", "release", "installed_release", "last_job"]) expect(Object.keys(p), `preset ${p.key} carries ${k}`).toContain(k);
  }
  const ew = presets.find((p) => p.key === "eval-worker");
  expect(ew, "eval-worker preset listed").toBeTruthy();
  expect(ew!.status, "eval-worker installed on the default env (do not uninstall)").toMatch(/installed|active/);
  const rel = ew!.release as { tag?: string; version?: string } | null;
  const lastJob = ew!.last_job as { id: string; type: string; status: string } | null;
  ledger(`obs31 eval-worker version=${ew!.version} release=${JSON.stringify(rel)} installed_release=${ew!.installed_release} last_job=${JSON.stringify(lastJob)}`);
  expect(ew!.version, "eval-worker version is the live runtime version").not.toBeNull();
  expect(rel && (rel.tag || rel.version), "eval-worker release populated").toBeTruthy();
  expect(ew!.installed_release, "eval-worker installed_release populated").toBeTruthy();
  expect(lastJob?.id, "eval-worker last_job").toBeTruthy();

  // (a) admin reads the job + log through the preset route; the generic route is recorded
  const adminRes = await page.request.get(`/api/system-agents/eval-worker/jobs/${lastJob!.id}`);
  const adminJob = await adminRes.json().catch(async () => ({ raw: await adminRes.text() }));
  const generic = await page.request.get(`/api/jobs/${lastJob!.id}`);
  const events = (adminJob.events ?? adminJob.log ?? []) as unknown[];
  await attach(testInfo, "eval-worker-last-job.json", adminJob);
  ledger(`obs31 admin GET /api/system-agents/eval-worker/jobs/${lastJob!.id} → ${adminRes.status()} status=${adminJob.status} type=${adminJob.type} events=${Array.isArray(events) ? events.length : "n/a"}; generic /api/jobs → ${generic.status()}`);
  expect(adminRes.status(), "admin reads the preset job").toBe(200);
  expect(adminJob.job_id ?? adminJob.id).toBe(lastJob!.id);
  expect(Array.isArray(events) && events.length, "job log events present").toBeGreaterThan(0);
  // the console renders it: /agents/new presets panel job line + VIEW LOG (recorded by spec 29's panel open)

  // (a) member → 403
  const stamp = new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "");
  const username = `${E2E_PREFIX}member-${stamp}`;
  const password = `E2e4!${stamp}-pw-${Math.random().toString(36).slice(2, 8)}`; // throwaway, never logged
  const reg = await browser.newContext({ storageState: { cookies: [], origins: [] }, ignoreHTTPSErrors: true, baseURL, locale: "en-US", viewport: { width: 1440, height: 900 } });
  const rp = await reg.newPage();
  await rp.goto("/");
  await rp.getByTestId("auth-tab-register").click();
  await rp.locator('input[autocomplete="username"]').fill(username);
  await rp.locator('input[autocomplete="email"]').fill(`${username}@example.invalid`);
  const pws = rp.locator('input[autocomplete="new-password"]');
  await pws.nth(0).fill(password);
  await pws.nth(1).fill(password);
  const regRes = rp.waitForResponse((r) => r.url().includes("/api/auth/register"));
  await pws.nth(1).press("Enter");
  const rr = await regRes;
  ledger(`obs31 register ${username} → ${rr.status()}`);
  expect(rr.status()).toBe(201);
  await reg.close();
  await page.goto("/v2/users");
  await expect(page.getByTestId("v2-users-table")).toBeVisible();
  await page.getByTestId(`v2-users-approve-${username}`).click();
  await expect(page.getByTestId("v2-users-approve")).toBeVisible();
  const chip = page.getByTestId("v2-users-approve-ws-default");
  if (await chip.isVisible().catch(() => false)) {
    const on = (await chip.getAttribute("aria-pressed")) === "true" || /\bon\b/.test((await chip.getAttribute("class")) ?? "");
    if (!on) await chip.click();
  }
  const patchRes = page.waitForResponse((r) => r.url().includes("/api/users/") && r.request().method() === "PATCH");
  await page.getByTestId("v2-users-approve-ok").click();
  const member = await (await patchRes).json();
  stateSet("member_31", { username, id: member.id });
  ledger(`obs31 approved member ${username} id=${member.id} role=${member.role}`);
  const { page: mp, context: mc } = await loginFresh(browser, username, password, baseURL);
  const memberRes = await mp.request.get(`/api/system-agents/eval-worker/jobs/${lastJob!.id}`);
  const memberText = (await memberRes.text()).slice(0, 200);
  const memberList = await mp.request.get("/api/system-agents");
  ledger(`obs31 member GET preset job → ${memberRes.status()} ${memberText.replace(/\n/g, " ")}; member GET /api/system-agents → ${memberList.status()}`);
  await mc.close();
  expect(memberRes.status(), "member is refused the preset job log").toBe(403);

  // N11-obs31: exactly one expense-report-writer skill record; status recorded, APPROVED only if it was approved before
  const records = await registryRecords(page.request);
  const erw = records.filter((r) => r.name === "expense-report-writer");
  const detail = erw[0] ? ((await (await page.request.get(`/api/registry/records/${erw[0].record_id}`)).json()) as Record<string, unknown>) : {};
  await attach(testInfo, "expense-report-writer-records.json", { list: erw, detail });
  const wasApproved = !!(detail.decided_by || detail.approved_at || /approv/i.test(String(detail.status_reason ?? "")));
  ledger(`obs31 expense-report-writer records=${erw.length} ${erw.map((r) => `${r.record_id.slice(0, 8)}:${r.type}:${r.status}`).join(",")} decided_by=${detail.decided_by ?? "-"} status_reason="${String(detail.status_reason ?? "").slice(0, 120)}" version=${detail.version ?? "-"} wasApproved=${wasApproved}`);
  await page.goto("/v2/registry");
  await expect(page.getByTestId("v2-registry-table")).toBeVisible({ timeout: 30_000 });
  await evidence(page, "obs31-registry-records");
  expect(erw.length, "exactly one expense-report-writer record (N11-obs31)").toBe(1);
  if (wasApproved) expect(erw[0].status, "a previously approved sample record stays/returns APPROVED (ruling a)").toBe("APPROVED");
  else ledger(`obs31 NOTE expense-report-writer is ${erw[0].status} with no earlier approval on record — acceptable per the lead's ruling (a); recorded`);
  ledger("obs31 end PASS");
});

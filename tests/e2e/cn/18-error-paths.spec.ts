import { expect, test } from "@playwright/test";
import { activeAgent, attach, e2eName, evidence, EVIDENCE_DIR, getAgent, ledger, listAgents, sleep, waitForJob } from "./helpers";

/**
 * Case 18 — error paths: (a) deploying with an invalid model id fails with a readable
 * error in the UI; (b) chatting with a deleted agent → 404 with a UI notice, not a blank
 * page; (c) a wrong password → 401 with a visible message.
 */
test("case 18a: invalid model id → deploy fails with a readable UI error", async ({ page }, testInfo) => {
  const name = e2eName("badmodel");
  ledger(`case18a start name=${name}`);
  await page.goto("/v2/agents");
  await page.getByTestId("v2-agent-new").click();
  await page.getByTestId("v2-agent-method-harness").click();
  await page.getByTestId("v2-agent-wizard-next").click();
  await page.getByTestId("v2-agent-name").fill(name);
  await page.getByTestId("v2-agent-prompt").fill("Error-path agent: this deployment is expected to fail.");
  await page.getByTestId("v2-agent-model").click();
  await page.getByTestId("v2-select-popup").getByRole("option", { name: /custom/i }).click();
  await page.getByTestId("v2-agent-model-custom").fill("global.nonexistent.model-v0");
  await evidence(page, "case18-invalid-model-configure");
  await page.getByTestId("v2-agent-wizard-next").click();
  // either the wizard refuses (validation error shown) or the deploy job fails
  const review = page.getByTestId("v2-agent-review");
  const createRes = page.waitForResponse((r) => r.url().endsWith("/api/agents") && r.request().method() === "POST", { timeout: 60_000 }).catch(() => null);
  if (await review.isVisible({ timeout: 5000 }).catch(() => false)) await page.getByTestId("v2-agent-wizard-submit").click();
  const cr = await createRes;
  if (cr && cr.status() >= 400) {
    const body = await cr.text();
    ledger(`case18a POST /api/agents → ${cr.status()} ${body.slice(0, 240)}`);
    const alert = page.locator('[role="alert"], .v2-alert.error, .v2-alert-error, .v2-field-error').first();
    await expect(alert).toBeVisible({ timeout: 15_000 });
    const text = (await alert.innerText()).trim();
    await evidence(page, "case18-invalid-model-refused");
    expect(text.length, "readable validation error").toBeGreaterThan(5);
    ledger(`case18a end PASS (refused at submit): ${text.slice(0, 160)}`);
    return;
  }
  await expect(page).toHaveURL(/view=detail&id=/, { timeout: 60_000 });
  const agentId = new URL(page.url()).searchParams.get("id")!;
  const agent = await getAgent(page.request, agentId);
  const jobId = ((agent.deployments as Array<{ job_id: string }>) ?? [])[0]?.job_id;
  const job = await waitForJob(page.request, jobId, { timeoutMs: 20 * 60 * 1000, intervalMs: 10_000 });
  await attach(testInfo, "bad-model-job.json", job);
  const after = await getAgent(page.request, agentId);
  ledger(`case18a job ${job.status} agent.status=${after.status} error=${String(job.error ?? after.error ?? "").slice(0, 200)}`);
  await page.reload();
  await expect(page.getByTestId("v2-agent-log")).toBeVisible({ timeout: 60_000 });
  const alert = page.locator(".v2-alert").filter({ hasText: /./ }).first();
  await expect(alert).toBeVisible({ timeout: 30_000 });
  const uiError = (await alert.innerText()).trim();
  await evidence(page, "case18-invalid-model-failed");
  expect(job.status).toBe("failed");
  expect(after.status).toBe("failed");
  expect(uiError.length, "UI shows a readable error").toBeGreaterThan(10);
  expect(uiError).not.toMatch(/^\s*\{/);
  ledger(`case18a end PASS (job failed) ui=${uiError.slice(0, 160)}`);
});

test("case 18b: chat with a deleted agent → 404 agent.not_found (API) + UI notice (no blank page)", async ({ page }) => {
  // victim: the BYOC agent from case 4 (not needed later), else any non-kept e2e3 agent
  const victim = (await activeAgent(page.request, "byoc")) ?? (await activeAgent(page.request, "studio"));
  expect(victim, "an expendable deployed agent (case 4 or 3) is required").toBeTruthy();
  ledger(`case18b start deleting ${victim!.name}`);
  await page.goto("/v2/agents");
  await page.getByTestId(`v2-agent-delete-${victim!.name}`).click();
  await page.getByTestId("v2-confirm-ok").click();
  for (let i = 0; i < 30; i++) {
    if (!(await listAgents(page.request)).some((a) => a.id === victim!.id)) break;
    await sleep(5000);
  }
  const chat = await page.request.post(`/api/chat/${victim!.id}`, { data: { prompt: "hello?", session_id: null } });
  const chatBody = await chat.json().catch(async () => ({ raw: await chat.text() }));
  ledger(`case18b POST /api/chat/<deleted> → ${chat.status()} ${JSON.stringify(chatBody).slice(0, 160)}`);
  const getDeleted = await page.request.get(`/api/agents/${victim!.id}`);
  const getBody = await getDeleted.json().catch(() => ({}));
  ledger(`case18b GET /api/agents/<deleted> → ${getDeleted.status()} ${JSON.stringify(getBody).slice(0, 160)}`);
  await page.goto(`/v2/chat?agent=${encodeURIComponent(victim!.id)}`);
  await expect(page.getByTestId("v2-shell")).toBeVisible();
  const notice = page.locator('[role="alert"], .v2-alert').first();
  await expect(notice).toBeVisible({ timeout: 30_000 });
  const text = (await notice.innerText()).trim();
  await evidence(page, "case18-chat-deleted-agent");
  expect(text.length).toBeGreaterThan(5);
  expect((await page.locator("body").innerText()).trim().length, "not a blank page").toBeGreaterThan(50);
  await page.goto(`/v2/agents?view=detail&id=${encodeURIComponent(victim!.id)}`);
  await expect(page.locator('[role="alert"], .v2-alert, [data-testid="notfound-body"]').first()).toBeVisible({ timeout: 30_000 });
  await evidence(page, "case18-detail-deleted-agent");
  // §4.1 / N11 r3-A: a deleted row reads as missing on every /api/agents/{id}* and /api/chat/{id}* route
  expect(chat.status(), "POST /api/chat/<deleted> → 404").toBe(404);
  expect(String((chatBody as { code?: string }).code ?? "")).toBe("agent.not_found");
  expect(getDeleted.status(), "GET /api/agents/<deleted> → 404").toBe(404);
  ledger(`case18b end PASS notice=${text.slice(0, 120)}`);
});

test("case 18c: wrong password → 401 with a visible message", async ({ browser }) => {
  const baseURL = test.info().project.use.baseURL as string;
  const ctx = await browser.newContext({ storageState: { cookies: [], origins: [] }, ignoreHTTPSErrors: true, baseURL, locale: "en-US", viewport: { width: 1440, height: 900 } });
  const p = await ctx.newPage();
  await p.goto("/");
  await p.locator('input[autocomplete="username"]').fill("admin");
  await p.locator('input[type="password"][autocomplete="current-password"]').fill("definitely-not-the-password");
  const res = p.waitForResponse((r) => r.url().includes("/api/auth/login"));
  await p.locator('input[type="password"][autocomplete="current-password"]').press("Enter");
  const r = await res;
  const alert = p.locator('[role="alert"]').first();
  await expect(alert).toBeVisible({ timeout: 30_000 });
  const text = (await alert.innerText()).trim();
  await p.screenshot({ path: `${EVIDENCE_DIR}/case18-wrong-password.png`, fullPage: true, mask: [p.locator('input[type="password"]')] });
  ledger(`case18c login → ${r.status()} ui=${text}`);
  expect(r.status()).toBe(401);
  expect(text.length).toBeGreaterThan(3);
  expect((await p.request.get("/api/auth/status").then((x) => x.json())).authenticated).toBe(false);
  await ctx.close();
  ledger("case18c end PASS");
});

import { expect, test } from "@playwright/test";
import { activeAgent, attach, createHarnessViaWizard, e2eName, evidence, getAgent, ledger, listAgents, loginFresh, sleep, waitForJob } from "./helpers";

/**
 * Case 12 (v3) — Users: self-registration → admin approval → member login → ownership
 * boundary (N11 r3-A): a member's DELETE / redeploy of the admin's agent → 403
 * `agent.forbidden` (the agent survives); the member's OWN agent can be created and deleted
 * (200); the admin sees every user and agent. Also records that `Agent.owner` is now the
 * login identity (N10v2-obs9).
 */
test("case 12: register → approve → member login → 403 on admin's agent, 200 on own agent, admin sees all", async ({ page, browser }, testInfo) => {
  const baseURL = test.info().project.use.baseURL as string;
  const stamp = new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "");
  const username = `e2e3-member-${stamp}`;
  const password = `E2e3!${stamp}-pw-${Math.random().toString(36).slice(2, 8)}`; // throwaway, never logged
  ledger(`case12 start member=${username}`);

  // ── register through the sign-in page (fresh context) ────────────────────
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
  const regBody = await rr.json();
  ledger(`case12 register → ${rr.status()} status=${regBody.status} requires_approval=${regBody.requires_approval}`);
  await expect(rp.getByTestId("register-success")).toBeVisible({ timeout: 30_000 });
  await evidence(rp, "case12-registered-pending");
  expect(rr.status()).toBe(201);
  expect(regBody.status).toBe("pending");
  await reg.close();

  // ── admin approves in the Users page ─────────────────────────────────────
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
  const pr = await patchRes;
  const member = await pr.json();
  ledger(`case12 approve → ${pr.status()} status=${member.status} role=${member.role} workspaces=${JSON.stringify(member.workspaces)}`);
  expect(pr.status()).toBe(200);
  expect(member.status).toBe("active");
  expect(member.role).toBe("member");
  await evidence(page, "case12-admin-approved");

  // ── member logs in (fresh context) ───────────────────────────────────────
  const { page: mp, context: mc } = await loginFresh(browser, username, password, baseURL);
  const mstatus = await (await mp.request.get("/api/auth/status")).json();
  await attach(testInfo, "member-auth-status.json", mstatus);
  expect(mstatus.role).toBe("member");
  await mp.goto("/v2");
  await expect(mp.getByTestId("v2-shell")).toBeVisible();
  const hrefs = await mp.locator("aside.v2-side a[href]").evaluateAll((as) => as.map((a) => (a as HTMLAnchorElement).getAttribute("href") ?? ""));
  ledger(`case12 member nav=${JSON.stringify(hrefs)}`);
  expect(hrefs.some((h) => h.startsWith("/v2/users")), "member must not see the Users page").toBe(false);
  const usersApi = await mp.request.get("/api/users");
  expect(usersApi.status(), "GET /api/users as member").toBe(403);
  await mp.goto("/v2/users");
  await expect(mp.getByTestId("users-forbidden-body").or(mp.getByTestId("notfound-body"))).toBeVisible({ timeout: 30_000 });
  await evidence(mp, "case12-member-users-forbidden");

  // ── member tries to delete / redeploy the admin's agent ──────────────────
  const victim = (await activeAgent(page.request, "harness")) ?? (await activeAgent(page.request, "gw")) ?? (await activeAgent(page.request, "byoc"));
  expect(victim, "an admin-owned e2e3 agent (case 2/5/4) must exist").toBeTruthy();
  const victimRow = await getAgent(page.request, victim!.id);
  ledger(`case12 victim=${victim!.name} owner=${victimRow.owner} (login identity expected: admin)`);
  const memberAgents = (await (await mp.request.get("/api/agents")).json()).agents as Array<Record<string, unknown>>;
  ledger(`case12 member sees ${memberAgents.length} agents (reads stay shared — r3-A D1)`);
  const del = await mp.request.delete(`/api/agents/${victim!.id}`);
  const delBody = await del.json().catch(async () => ({ raw: await del.text() }));
  ledger(`case12 member DELETE admin's agent → ${del.status()} ${JSON.stringify(delBody).slice(0, 200)}`);
  const redeploy = await mp.request.post(`/api/agents/${victim!.id}/redeploy`, { data: victimRow.spec });
  const redeployBody = await redeploy.json().catch(async () => ({ raw: await redeploy.text() }));
  ledger(`case12 member POST redeploy admin's agent → ${redeploy.status()} ${JSON.stringify(redeployBody).slice(0, 200)}`);
  await attach(testInfo, "member-mutations-on-admin-agent.json", { delete: { status: del.status(), body: delBody }, redeploy: { status: redeploy.status(), body: redeployBody } });
  const stillThere = (await listAgents(page.request)).some((a) => a.id === victim!.id && a.status === "active");
  await mp.goto(`/v2/agents?view=detail&id=${encodeURIComponent(victim!.id)}`);
  await expect(mp.getByTestId("v2-agent-basic")).toBeVisible({ timeout: 30_000 });
  await evidence(mp, "case12-member-agent-detail");

  // ── member's OWN agent: create → deploy → delete → 200 ────────────────────
  const ownName = e2eName("member-own");
  const t0 = Date.now();
  const ownId = await createHarnessViaWizard(mp, { name: ownName, prompt: "Member-owned e2e3 agent; answer briefly.", evidencePrefix: "case12-own" });
  const ownRow = await getAgent(mp.request, ownId);
  ledger(`case12 member created own agent ${ownName} (${ownId}) owner=${ownRow.owner}`);
  const jobId = ((ownRow.deployments as Array<{ job_id: string }>) ?? [])[0]?.job_id;
  const job = await waitForJob(mp.request, jobId, { timeoutMs: 20 * 60 * 1000 });
  const ownSeconds = Math.round((Date.now() - t0) / 1000);
  ledger(`case12 own agent deploy ${job.status} in ${ownSeconds}s`);
  await attach(testInfo, "member-own-deploy-job.json", job);
  const ownDel = await mp.request.delete(`/api/agents/${ownId}`);
  const ownDelBody = await ownDel.json().catch(async () => ({ raw: await ownDel.text() }));
  ledger(`case12 member DELETE own agent → ${ownDel.status()} removed=${JSON.stringify(ownDelBody.removed)} failed=${JSON.stringify(ownDelBody.failed)}`);
  await attach(testInfo, "member-own-delete.json", ownDelBody);
  for (let i = 0; i < 30; i++) {
    if (!(await listAgents(page.request)).some((a) => a.id === ownId)) break;
    await sleep(5000);
  }
  await mp.goto("/v2/agents");
  await evidence(mp, "case12-member-own-agent-deleted");
  await mc.close();

  // ── admin sees all users and agents ──────────────────────────────────────
  const adminUsers = await (await page.request.get("/api/users")).json();
  expect(adminUsers.items.some((u: { username: string }) => u.username === username)).toBe(true);
  await page.goto("/v2/users");
  await expect(page.getByTestId(`v2-users-open-${username}`).or(page.getByText(username).first())).toBeVisible();
  await evidence(page, "case12-admin-users-all");
  ledger(`case12 end delete_status=${del.status()} redeploy_status=${redeploy.status()} agent_still_active=${stillThere} own_delete=${ownDel.status()}`);
  expect(del.status(), "member deleting another user's agent must be 403").toBe(403);
  expect(String((delBody as { code?: string }).code ?? ""), "error code").toBe("agent.forbidden");
  expect(redeploy.status(), "member redeploying another user's agent must be 403").toBe(403);
  expect(stillThere).toBe(true);
  expect(String(ownRow.owner), "owner of the member's agent is the member").toBe(username);
  expect(job.status, "member's own deploy succeeds").toBe("succeeded");
  expect(ownDel.status(), "member deleting its own agent → 200").toBe(200);
  expect(ownDelBody.failed ?? []).toEqual([]);
});

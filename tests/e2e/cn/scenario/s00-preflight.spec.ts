import { expect, test } from "@playwright/test";
import { ledger, registryRecords } from "../helpers";
import { EVAL_WORKER_PRESET, GATEWAY_TARGET, HR_SKILL, preset, record, runStamp, SCN_PREFIX, snap, stateSet, stepGate } from "./scn";

/**
 * s00 — preconditions of the storyline on a fresh default environment (plan §2).
 *
 * Nothing is created except the second test identity (a member account used by s05 for the
 * ownership boundary). Every other check is read-only: health, the 13 capabilities, the default
 * workspace `ready`, the Global Bedrock API-key provider `ready`, the `hr-database` gateway
 * target registered + APPROVED by bootstrap (no `sync-defaults`), the evaluation-worker preset
 * `active`, Skill Lab provisioned with OpenCode as the only executor, and the HR skill
 * `expense-report-writer` seeded in the Registry.
 */
test.describe.configure({ mode: "serial" });

test("s00a — environment: health, capabilities, workspace, identity provider, gateway target, worker presets, Skill Lab", async ({ page, request }, testInfo) => {
  stepGate("s00");
  const stamp = runStamp();
  ledger(`s00 start stamp=${stamp} base=${testInfo.project.use.baseURL}`);

  const health = await request.get("/api/health", { timeout: 60_000 });
  const hb = await health.json();
  expect(health.status(), "GET /api/health").toBe(200);
  expect(hb.status).toBe("ok");
  expect(hb.region).toBe("cn-northwest-1");

  const caps = (await (await page.request.get("/api/capabilities", { timeout: 60_000 })).json()) as { features: string[] };
  await record(testInfo, "capabilities.json", caps, `${caps.features.length} features`);
  expect(caps.features.length, "≥ 13 features on the default environment (N10 ten + skill_lab + evaluation + optimization; + policy since D20)").toBeGreaterThanOrEqual(13);
  expect(caps.features, "capability policy (D20 engine registered)").toContain("policy");
  for (const f of ["skill_lab", "evaluation", "optimization"]) expect(caps.features, `capability ${f}`).toContain(f);

  const ws = (await (await page.request.get("/api/workspaces", { timeout: 60_000 })).json()) as { workspaces: Array<Record<string, unknown>> };
  const def = ws.workspaces.find((w) => w.is_default)!;
  expect(def.bootstrap_status, "default workspace bootstrapped").toBe("ready");
  expect(def.region).toBe("cn-northwest-1");

  const conns = (await (await page.request.get("/api/identity/connections", { timeout: 60_000 })).json()) as { connections: Array<{ name: string; kind: string; status: string }> };
  const provider = conns.connections.find((c) => c.name === "launchpad-global-bedrock");
  expect(provider, "Global Bedrock API-key provider").toBeTruthy();
  expect(provider!.kind).toBe("api_key");
  expect(provider!.status).toBe("ready");

  // gateway target: registered + APPROVED by bootstrap (REQUIREMENTS §4.1 case 5 — no sync-defaults)
  const records = await registryRecords(page.request);
  const hr = records.find((r) => r.name === GATEWAY_TARGET);
  await record(testInfo, "registry-records.json", records, `${records.length} records`);
  expect(hr, `${GATEWAY_TARGET} record exists after bootstrap`).toBeTruthy();
  expect(hr!.status, `${GATEWAY_TARGET} APPROVED out of the box`).toBe("APPROVED");
  const skill = records.find((r) => r.name === HR_SKILL);
  expect(skill, `${HR_SKILL} seeded in the Registry`).toBeTruthy();

  // presets: evaluation worker active; Skill Lab provisioned on OpenCode
  const worker = await preset(page.request, EVAL_WORKER_PRESET);
  await record(testInfo, "eval-worker-preset.json", worker, `status=${worker?.status} release=${worker?.installed_release}`);
  expect(String(worker?.status), "eval-worker preset installed").toBe("active");
  const sl = (await (await page.request.get("/api/skill-lab/status", { timeout: 60_000 })).json()) as Record<string, unknown>;
  await record(testInfo, "skill-lab-status.json", sl, `provisioned=${sl.provisioned} backends=${JSON.stringify(sl.target_backends)}`);
  expect(sl.provisioned, "Skill Lab provisioned").toBe(true);
  expect(sl.target_backends, "OpenCode is the only executor").toEqual(["opencode_exec"]);

  // no leftovers from an earlier storyline run (the cleanup spec must have run)
  const leftovers = records.filter((r) => r.name.startsWith(SCN_PREFIX)).map((r) => r.name);
  ledger(`s00 leftover ${SCN_PREFIX} registry records=${JSON.stringify(leftovers)}`);
  expect(leftovers, "a fresh environment carries no e2es- registry record").toEqual([]);

  await page.goto("/v2");
  await expect(page.getByTestId("v2-kpi-agents")).toBeVisible();
  await snap(page, "s00-overview", `${caps.features.length} features`, { key: true });
  ledger("s00a PASS");
});

test("s00b — second identity: register the member account, admin approves it (used by s05 for the ownership boundary)", async ({ page, browser }, testInfo) => {
  stepGate("s00");
  const baseURL = testInfo.project.use.baseURL as string;
  const username = `${SCN_PREFIX}member-${runStamp()}`;
  // throwaway credential of a test account; lives only in the git-ignored state file and is deleted by the cleanup
  const password = `Scn!${runStamp()}-${Math.random().toString(36).slice(2, 10)}`;

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
  const regBody = (await rr.json()) as { status?: string };
  ledger(`s00 register ${username} → ${rr.status()} status=${regBody.status}`);
  await expect(rp.getByTestId("register-success")).toBeVisible({ timeout: 30_000 });
  await reg.close();
  expect(rr.status()).toBe(201);
  expect(regBody.status).toBe("pending");

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
  const member = (await pr.json()) as { id?: string; status?: string; role?: string };
  ledger(`s00 approve ${username} → ${pr.status()} status=${member.status} role=${member.role}`);
  await snap(page, "s00-member-approved", username);
  expect(pr.status()).toBe(200);
  expect(member.status).toBe("active");
  expect(member.role).toBe("member");
  stateSet("member", { username, password, id: member.id });
  ledger(`s00b PASS member=${username}`);
});

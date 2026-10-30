import { expect, test } from "@playwright/test";
import { attach, awsRead, evidence, ledger, openClassicPresets, registryRecords, sleep, stateSet, waitForJob } from "./helpers";

const PRESET = "aws-agent-solution-architect";

/**
 * N10v3-bug6 + N11-r4-bug1 re-check (round 4): uninstalling the architect system preset
 * completes (`not_installed`), its execution role is gone (read-only IAM) and BOTH of its
 * registry records (A2A + the protected AGENT_SKILLS one) are removed by the uninstall's
 * fourth `registry` step. The `/agents/new` presets panel (kept after r13-a) must render its Uninstall button
 * (round 3 only ever saw the AuthGate spinner after a reload — `openClassicPresets` now
 * attributes a stall through `data-testid=v2-auth-loading` / `data-reason`). Case 11 installs
 * the preset through the console; this spec installs it too if needed.
 */
test("bug6: install architect preset (console) → uninstall → not_installed, role gone, A2A + AGENT_SKILLS records gone", async ({ page }, testInfo) => {
  const presets = async () => (((await (await page.request.get("/api/system-agents")).json()).presets ?? []) as Array<Record<string, unknown>>);
  let preset = (await presets()).find((p) => p.key === PRESET)!;
  ledger(`bug6 start preset status=${preset.status} operation=${JSON.stringify(preset.operation).slice(0, 200)} last_job=${JSON.stringify(preset.last_job).slice(0, 160)}`);
  if (preset.status === "not_installed") {
    const open = await openClassicPresets(page, "bug6-install");
    stateSet("authgate_case29_install", open);
    const install = page.getByTestId(`install-${PRESET}`);
    await expect(install, `install button (panel ready ${open.readyMs}ms, spinner samples ${open.stall.length})`).toBeVisible({ timeout: 60_000 });
    const res = page.waitForResponse((r) => r.url().includes(`/api/system-agents/${PRESET}/install`) && r.request().method() === "POST", { timeout: 60_000 });
    await install.click();
    const ok = page.getByTestId("v2-confirm-ok");
    if (await ok.isVisible({ timeout: 2000 }).catch(() => false)) await ok.click();
    const dialog = page.getByRole("alertdialog");
    if (await dialog.isVisible({ timeout: 2000 }).catch(() => false)) await dialog.getByRole("button").last().click();
    const r = await res;
    const body = await r.json().catch(() => ({}));
    ledger(`bug6 install → ${r.status()} ${JSON.stringify(body).slice(0, 200)}`);
    if (body.job_id) {
      const job = await waitForJob(page.request, body.job_id, { timeoutMs: 20 * 60 * 1000 }).catch(async () => (await (await page.request.get(`/api/system-agents/${PRESET}/jobs/${body.job_id}`)).json()) as { status: string; error?: string; job_id: string });
      await attach(testInfo, "preset-install-job.json", job);
      expect(job.status, `install failed: ${job.error ?? ""}`).toBe("succeeded");
    }
    for (let i = 0; i < 60; i++) {
      preset = (await presets()).find((p) => p.key === PRESET)!;
      if (preset.status === "installed" || preset.status === "active") break;
      await sleep(10_000);
    }
    await evidence(page, "bug6-preset-installed");
  }
  const agentId = String(preset.agent_id ?? (preset.agent as { id?: string } | undefined)?.id ?? "");
  const roles = awsRead(`iam list-roles --query "Roles[?starts_with(RoleName, 'launchpad-agent-${PRESET}')].RoleName"`);
  const isPresetRecord = (r: { name: string }) => r.name === PRESET || r.name.includes("solution-architect");
  const recordsBefore = (await registryRecords(page.request)).filter(isPresetRecord);
  await attach(testInfo, "preset-installed.json", { preset, roles, records: recordsBefore });
  ledger(`bug6 installed status=${preset.status} agent=${agentId} version=${preset.version} release=${JSON.stringify(preset.release).slice(0, 120)} installed_release=${preset.installed_release} roles=${roles.replace(/\s/g, "")} records=${recordsBefore.map((r) => `${r.name}:${r.type}:${r.status}`).join(",")}`);
  expect(["installed", "active"]).toContain(String(preset.status));
  expect(recordsBefore.length, "an installed preset registers its A2A + AGENT_SKILLS records").toBeGreaterThanOrEqual(2);

  // ── the /agents/new presets panel renders for the installed preset (Uninstall button) ──
  const open = await openClassicPresets(page, "bug6-panel");
  await attach(testInfo, "classic-presets-open.json", open);
  stateSet("authgate_case29_panel", open);
  const un = page.getByTestId(`uninstall-${PRESET}`);
  const unVisible = await un.isVisible({ timeout: 30_000 }).catch(() => false);
  const releaseLine = await page.getByTestId("preset-release").first().innerText().catch(() => "");
  ledger(`bug6 panel: uninstall button visible=${unVisible} release line="${releaseLine.replace(/\n/g, " ").slice(0, 160)}"`);
  await evidence(page, "bug6-panel-installed");

  // ── uninstall through the console (button) or the product API ───────────
  let via = "ui";
  let status = -1;
  let body: Record<string, unknown> = {};
  const t0 = Date.now();
  if (unVisible) {
    const res = page.waitForResponse((r) => r.url().includes(`/api/system-agents/${PRESET}`) && r.request().method() === "DELETE", { timeout: 60_000 });
    await un.click();
    const ok = page.getByTestId("v2-confirm-ok");
    if (await ok.isVisible({ timeout: 2000 }).catch(() => false)) await ok.click();
    const dialog = page.getByRole("alertdialog");
    if (await dialog.isVisible({ timeout: 2000 }).catch(() => false)) await dialog.getByRole("button").last().click();
    const r = await res;
    status = r.status();
    body = await r.json().catch(() => ({}));
  } else {
    via = `api (no Uninstall button rendered — panel ready=${open.readyMs}ms, spinner reasons=${JSON.stringify([...new Set(open.stall.map((s) => s.reason))])})`;
    const r = await page.request.delete(`/api/system-agents/${PRESET}`);
    status = r.status();
    body = await r.json().catch(() => ({}));
  }
  ledger(`bug6 uninstall via ${via} → ${status} job=${body.job_id} ${JSON.stringify(body).slice(0, 200)}`);
  await evidence(page, "bug6-uninstall-requested");
  let final = preset;
  while (Date.now() - t0 < 15 * 60 * 1000) {
    final = (await presets()).find((p) => p.key === PRESET)!;
    if (final.status === "not_installed") break;
    await sleep(10_000);
  }
  const seconds = Math.round((Date.now() - t0) / 1000);
  await attach(testInfo, "preset-after-uninstall.json", final);
  // N12-obs31 (a): the uninstall job stays readable through the preset route after the row is gone
  let job: Record<string, unknown> | null = null;
  if (body.job_id) {
    const jr = await page.request.get(`/api/system-agents/${PRESET}/jobs/${body.job_id}`);
    job = jr.ok() ? ((await jr.json()) as Record<string, unknown>) : { http_status: jr.status(), text: (await jr.text()).slice(0, 200) };
    await attach(testInfo, "preset-uninstall-job.json", job);
  }
  const teardown = ((job?.payload as { teardown?: Record<string, string[]> } | undefined)?.teardown ?? {}) as Record<string, string[]>;
  const rolesAfter = awsRead(`iam list-roles --query "Roles[?starts_with(RoleName, 'launchpad-agent-${PRESET}')].RoleName"`);
  const recordsAfter = (await registryRecords(page.request)).filter(isPresetRecord);
  const reopen = await openClassicPresets(page, "bug6-after");
  stateSet("authgate_case29_after", reopen);
  await evidence(page, "bug6-preset-after-uninstall");
  ledger(`bug6 after ${seconds}s status=${final.status} operation=${JSON.stringify(final.operation).slice(0, 300)} job=${job?.status} removed=${JSON.stringify(final.operation ? (final.operation as { removed?: string[] }).removed : teardown.removed)} roles=${rolesAfter.replace(/\s/g, "")} records=${recordsAfter.map((r) => `${r.name}:${r.type}:${r.status}`).join(",") || "NONE"}`);
  expect([200, 202]).toContain(status);
  expect(final.status, "preset back to not_installed").toBe("not_installed");
  expect(job?.status, "uninstall job readable through the preset route and succeeded").toBe("succeeded");
  expect(rolesAfter.replace(/\s/g, ""), "preset execution role gone").toMatch(/^\[\]$|^$/);
  expect(recordsAfter.filter((r) => r.type === "A2A").length, "A2A record gone").toBe(0);
  expect(recordsAfter.filter((r) => /SKILL/i.test(r.type)).length, "protected AGENT_SKILLS record gone (N10v3-bug6 / N11-r4-bug1)").toBe(0);
  expect(recordsAfter.length, "no preset registry record left").toBe(0);
  expect(unVisible, "presets panel rendered the Uninstall button for the installed preset").toBe(true);
  ledger(`bug6 end PASS uninstall ${seconds}s via ${via}`);
});

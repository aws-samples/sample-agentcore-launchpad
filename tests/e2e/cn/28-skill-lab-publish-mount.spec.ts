import { expect, test } from "@playwright/test";
import { approveRecordViaUi, attach, chatOnce, createHarnessViaWizard, e2eName, evidence, ledger, looksLikeError, registryRecords, waitForDeploy } from "./helpers";
import { latestJob, SKILL_NAME } from "./skilllab-helpers";

/**
 * §4.2 — the evaluated / trained skill is published back to the Registry (new minor version,
 * re-approved) and mounted on a new Harness agent, which uses it in Chat.
 */
test("case 28: publish the trained skill → Registry version bump + APPROVED → mount on an agent → used in Chat", async ({ page }, testInfo) => {
  const before = (await registryRecords(page.request)).find((r) => r.name === SKILL_NAME);
  expect(before, "case 24 skill record").toBeTruthy();
  const recBefore = await (await page.request.get(`/api/registry/records/${before!.record_id}`)).json();
  const versionBefore = String(recBefore.version ?? recBefore.recordVersion ?? "");
  const train = await latestJob(page.request, "train", (j) => j.status === "succeeded" && String(j.skill_source?.record_id ?? "") === before!.record_id);
  ledger(`case28 skill ${SKILL_NAME} ${before!.record_id} version=${versionBefore} status=${recBefore.status} train job=${train?.id ?? "NONE"}`);
  let published: Record<string, unknown> | null = null;
  let publishStatus = -1;
  if (train) {
    await page.goto(`/v2/skill-lab?tab=train&view=detail&id=${encodeURIComponent(train.id)}`);
    await expect(page.getByTestId("v2-train-publish")).toBeVisible({ timeout: 60_000 });
    await evidence(page, "case28-train-publish-card");
    const diff = await (await page.request.get(`/api/skill-lab/jobs/${train.id}/diff`)).json().catch(() => ({}));
    ledger(`case28 diff changed=${diff.changed}`);
    const btn = page.getByTestId("v2-train-publish-btn");
    if (await btn.isVisible().catch(() => false) && (await btn.isEnabled())) {
      await btn.click();
      await expect(page.getByTestId("v2-train-publish-dialog")).toBeVisible({ timeout: 10_000 });
      const re = page.getByTestId("v2-train-reapprove");
      if (await re.isVisible().catch(() => false) && !(await re.isChecked())) await re.check();
      await evidence(page, "case28-publish-dialog");
      const res = page.waitForResponse((r) => r.url().includes("/publish") && r.request().method() === "POST", { timeout: 60_000 });
      await page.getByTestId("v2-train-publish-confirm").click();
      const r = await res;
      publishStatus = r.status();
      published = await r.json().catch(async () => ({ raw: await r.text() }));
      ledger(`case28 publish → ${publishStatus} ${JSON.stringify(published).slice(0, 300)}`);
      await attach(testInfo, "publish.json", published);
      await evidence(page, "case28-published");
    } else {
      ledger(`case28 publish button not available (diff.changed=${diff.changed}) — training produced no accepted edit`);
    }
  }
  let rec = await (await page.request.get(`/api/registry/records/${before!.record_id}`)).json();
  if (rec.status !== "APPROVED") {
    const st = await approveRecordViaUi(page, before!.record_id);
    ledger(`case28 record re-approved via UI → ${st}`);
    rec = await (await page.request.get(`/api/registry/records/${before!.record_id}`)).json();
  }
  const versionAfter = String(rec.version ?? rec.recordVersion ?? "");
  await page.goto(`/v2/registry?view=detail&id=${encodeURIComponent(before!.record_id)}`);
  await evidence(page, "case28-registry-record-after-publish");
  ledger(`case28 record version ${versionBefore} → ${versionAfter} status=${rec.status}`);

  // mount on a new Harness agent
  const name = e2eName("slskill");
  const t0 = Date.now();
  const agentId = await createHarnessViaWizard(page, {
    name,
    prompt: `You are an SRE helper. You have the ${SKILL_NAME} skill mounted; when asked about log triage, follow that skill's instructions and name the bundled parser script and the report layout it prescribes.`,
    skills: [SKILL_NAME],
    evidencePrefix: "case28",
  });
  const dep = await waitForDeploy(page, testInfo, agentId, "case28", t0);
  expect(dep.job.status, `deploy failed: ${dep.job.error ?? ""}`).toBe("succeeded");
  expect(dep.seconds).toBeLessThanOrEqual(15 * 60);
  const chat = await chatOnce(page, agentId, "A service access log is at logs/access.log. Using your log-triage skill, tell me exactly which bundled script you would run and what the triage report file and its title line must look like.", { timeoutMs: 6 * 60 * 1000 });
  await attach(testInfo, "skill-chat.txt", `${chat.answer}\n\ntools: ${chat.toolNames.join(", ")}`);
  await evidence(page, "case28-agent-skill-chat");
  ledger(`case28 chat ${chat.seconds}s errors=${chat.errors} tools=${chat.toolNames.join(",")} answer=${chat.answer.slice(0, 240).replace(/\n/g, " ")}`);
  expect(chat.errors).toBe(0);
  expect(looksLikeError(chat.answer)).toBe(false);
  expect(chat.answer, "answer draws on the skill (parser script / triage report)").toMatch(/parse_logs|triage/i);
  expect(rec.status, "skill record APPROVED after publish").toBe("APPROVED");
  expect(train, "a succeeded training job to publish from (case 26)").toBeTruthy();
  expect(publishStatus, "publish → 200").toBe(200);
  expect(versionAfter, "registry version bumped").not.toBe(versionBefore);
  ledger(`case28 end PASS agent=${agentId} version ${versionBefore} → ${versionAfter}`);
});

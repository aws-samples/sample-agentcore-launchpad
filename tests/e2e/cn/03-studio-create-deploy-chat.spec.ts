import { expect, test } from "@playwright/test";
import { attach, chatOnce, e2eName, evidence, ledger, listAgents, reusableAgent, waitForDeploy, type DeployResult } from "./helpers";

/** Case 3 — 方式C: Strands Studio canvas (sample flow) → generated code → publish → deploy → Chat. */
test("case 3: Studio sample flow → publish → deploy → Chat answers", async ({ page }, testInfo) => {
  const name = e2eName("studio");
  ledger(`case3 start name=${name}`);
  let agentId: string | undefined;
  let dep: DeployResult = { seconds: 0 } as DeployResult;
  const reuse = await reusableAgent(page.request, "studio");
  if (reuse) {
    agentId = reuse.id;
    ledger(`case3 reusing active agent ${reuse.name} (${agentId}) — deploy evidence from the earlier run`);
  } else {
  await page.goto("/create/studio");
  await page.getByRole("button", { name: /Samples/ }).click();
  const gallery = page.locator(".studio-samples-box");
  await expect(gallery).toBeVisible();
  await expect(gallery.getByText("Single Agent", { exact: true })).toBeVisible();
  await evidence(page, "case03-studio-gallery");
  // the first "Load" belongs to the first basic sample (Single Agent — one GLM node)
  await gallery.getByRole("button", { name: "Load" }).first().click();
  const replace = page.getByRole("button", { name: "Load sample" });
  if (await replace.isVisible().catch(() => false)) await replace.click();
  await expect(gallery).toBeHidden();
  await evidence(page, "case03-studio-canvas");

  await page.getByRole("button", { name: /^▲?\s*Publish$/ }).first().click();
  const dialog = page.locator(".confirm-box");
  await expect(dialog).toBeVisible();
  await dialog.locator("#studio-name").fill(name);
  await evidence(page, "case03-studio-publish-dialog");
  const t0 = Date.now();
  await dialog.getByRole("button", { name: /Publish/ }).click();
  // the canvas is replaced by the launch view; resolve the new agent through the API
  for (let i = 0; i < 20 && !agentId; i++) {
    agentId = (await listAgents(page.request)).find((a) => a.name === name)?.id as string | undefined;
    if (!agentId) await page.waitForTimeout(1500);
  }
  expect(agentId, "studio agent should exist after publish").toBeTruthy();
  ledger(`case3 agent created id=${agentId}`);
  await evidence(page, "case03-studio-launch-view");

  dep = await waitForDeploy(page, testInfo, agentId!, "case3", t0);
  expect(dep.job.status, `deploy job failed: ${dep.job.error ?? ""}`).toBe("succeeded");
  expect(dep.agent.status).toBe("active");
  }

  const chat = await chatOnce(page, agentId!, "In one short sentence, say hello and name the model family you are. Then compute 7*6.");
  await attach(testInfo, "chat-answer.txt", chat.answer);
  ledger(`case3 chat answered in ${chat.seconds}s: ${chat.answer.slice(0, 200).replace(/\n/g, " ")}`);
  await evidence(page, "case03-chat");
  expect(chat.answer.length).toBeGreaterThan(5);
  expect(chat.answer).toMatch(/42/);
  ledger(`case3 end PASS agent=${agentId} session=${chat.sessionId} deploy=${dep.seconds}s`);
});

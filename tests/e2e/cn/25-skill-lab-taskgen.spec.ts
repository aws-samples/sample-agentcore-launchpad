import { expect, test } from "@playwright/test";
import { attach, E2E_PREFIX, evidence, ledger, registryRecords } from "./helpers";
import { captureFailure, e2eStamp, latestJob, pickSkillRow, SKILL_NAME, tasksets, waitForSkillLabJob } from "./skilllab-helpers";

/**
 * §4.2 — taskgen pipeline: the Taskgen wizard (skill → count → guidance) submits an
 * asynchronous job to the AgentCore Runtime worker (OpenCode + Kimi K3); it succeeds within
 * 30 min, its results render in the review editor, and the reviewed tasks can be saved as a
 * new task set.
 */
test("case 25: taskgen → worker job succeeds ≤ 30 min → review page renders → import as task set", async ({ page }, testInfo) => {
  test.setTimeout(45 * 60 * 1000);
  const skill = (await registryRecords(page.request)).find((r) => r.name === SKILL_NAME);
  expect(skill, "case 24 must have registered the skill").toBeTruthy();
  let job = process.env.N10_REUSE ? await latestJob(page.request, "taskgen", (j) => j.status === "succeeded") : undefined;
  const t0 = Date.now();
  if (!job) {
    await page.goto("/v2/skill-lab?tab=tasksets&view=gen-new");
    await expect(page.getByTestId("v2-taskgen-submit")).toBeVisible({ timeout: 60_000 });
    await pickSkillRow(page, "v2-taskgen-skills", skill!.record_id);
    await page.getByTestId("v2-taskgen-count").fill("2");
    await page.getByTestId("v2-taskgen-guidance").fill("Generate log-triage tasks in the style of the demo: a small access log under logs/ and a rubric that checks the triage report file, title line, parser stamp and statistics.");
    const backend = await page.getByTestId("v2-taskgen-backend").innerText();
    const model = await page.getByTestId("v2-taskgen-target-model").inputValue();
    await evidence(page, "case25-taskgen-wizard");
    const res = page.waitForResponse((r) => r.url().endsWith("/api/skill-lab/jobs") && r.request().method() === "POST", { timeout: 60_000 });
    await page.getByTestId("v2-taskgen-submit").click();
    const r = await res;
    const body = await r.json().catch(async () => ({ raw: await r.text() }));
    ledger(`case25 submit → ${r.status()} backend="${backend.trim()}" model=${model} job=${JSON.stringify(body).slice(0, 240)}`);
    await attach(testInfo, "taskgen-submit.json", body);
    expect(r.status(), `POST /api/skill-lab/jobs: ${JSON.stringify(body).slice(0, 200)}`).toBe(201);
    await expect(page).toHaveURL(/view=gen&id=/, { timeout: 30_000 });
    await evidence(page, "case25-taskgen-detail-running");
    job = await waitForSkillLabJob(page.request, body.id, "case25", 30 * 60 * 1000);
  }
  const seconds = Math.round((Date.now() - t0) / 1000);
  await attach(testInfo, "taskgen-job.json", job);
  ledger(`case25 job ${job.id} ${job.status} after ${seconds}s params=${JSON.stringify(job.params).slice(0, 200)}`);
  if (job.status !== "succeeded") await captureFailure(page.request, testInfo, job, "case25");
  await page.goto(`/v2/skill-lab?tab=tasksets&view=gen&id=${encodeURIComponent(job.id)}`);
  await expect(page.getByTestId("v2-taskgen-log").or(page.getByTestId("v2-taskgen-review"))).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case25-taskgen-detail-final");
  expect(job.status, `taskgen job must succeed: ${job.error ?? ""}`).toBe("succeeded");
  expect(seconds).toBeLessThanOrEqual(30 * 60);

  const results = await (await page.request.get(`/api/skill-lab/jobs/${job.id}/results`)).json();
  await attach(testInfo, "taskgen-results.json", results);
  const generated = (results.tasks ?? []) as unknown[];
  ledger(`case25 results type=${results.type} tasks=${generated.length}`);
  expect(generated.length, "≥ 1 generated task").toBeGreaterThanOrEqual(1);
  await expect(page.getByTestId("v2-taskgen-review")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("v2-taskgen-row-0")).toBeVisible();
  await evidence(page, "case25-taskgen-review");
  // import the reviewed tasks as a new task set
  const name = `${E2E_PREFIX}gen-${e2eStamp()}`;
  await page.getByTestId("v2-taskgen-import-name").fill(name);
  const imp = page.waitForResponse((r) => r.url().includes("/import-taskset") && r.request().method() === "POST", { timeout: 60_000 });
  await page.getByTestId("v2-taskgen-import").click();
  const ir = await imp;
  const ib = await ir.json().catch(async () => ({ raw: await ir.text() }));
  ledger(`case25 import-taskset → ${ir.status()} ${JSON.stringify(ib).slice(0, 200)}`);
  await attach(testInfo, "taskgen-import.json", ib);
  expect(ir.status()).toBe(201);
  const row = (await tasksets(page.request)).find((t) => t.name === name);
  expect(row, "imported task set listed").toBeTruthy();
  await page.goto(`/v2/skill-lab?tab=tasksets&view=detail&id=${encodeURIComponent(row!.id)}`);
  await expect(page.getByText(name).first()).toBeVisible({ timeout: 30_000 });
  await evidence(page, "case25-imported-taskset");
  ledger(`case25 end PASS job=${job.id} ${seconds}s tasks=${generated.length} taskset=${row!.id}`);
});

import { expect, test } from "@playwright/test";
import { attach, evidence, ledger, registryRecords, rolloutHealth } from "./helpers";
import { captureFailure, latestJob, pickSkillRow, selectV2, SKILL_NAME, TASKSET_NAME, tasksets, waitForSkillLabJob } from "./skilllab-helpers";

/**
 * §4.2 — train pipeline: Train wizard (registry skill → task set → 1 epoch) → asynchronous
 * worker job succeeds ≤ 30 min → `train-summary` has steps → the training curve + KPIs render.
 */
test("case 26: train (1 epoch) → worker job succeeds ≤ 30 min → training curve renders with data", async ({ page }, testInfo) => {
  test.setTimeout(50 * 60 * 1000);
  const skill = (await registryRecords(page.request)).find((r) => r.name === SKILL_NAME);
  const ts = (await tasksets(page.request)).find((t) => t.name === TASKSET_NAME);
  expect(skill && ts, "case 24 must have created the skill and the task set").toBeTruthy();
  let job = process.env.N10_REUSE ? await latestJob(page.request, "train", (j) => j.status === "succeeded") : undefined;
  const t0 = Date.now();
  if (!job) {
    await page.goto("/v2/skill-lab?tab=train&view=new");
    await expect(page.getByTestId("v2-train-submit")).toBeVisible({ timeout: 60_000 });
    await pickSkillRow(page, "v2-train-source-skills", skill!.record_id);
    await selectV2(page, "v2-train-taskset", new RegExp(TASKSET_NAME));
    const epochs = page.getByTestId("v2-train-epochs");
    if (await epochs.isVisible().catch(() => false)) await epochs.fill("1");
    const estimate = await page.getByTestId("v2-train-estimate").innerText().catch(() => "");
    const trainable = await page.getByTestId("v2-train-trainable").innerText().catch(() => "");
    await evidence(page, "case26-train-wizard");
    const res = page.waitForResponse((r) => r.url().endsWith("/api/skill-lab/jobs") && r.request().method() === "POST", { timeout: 60_000 });
    await page.getByTestId("v2-train-submit").click();
    const r = await res;
    const body = await r.json().catch(async () => ({ raw: await r.text() }));
    ledger(`case26 submit → ${r.status()} job=${JSON.stringify(body).slice(0, 240)} estimate="${estimate.replace(/\n/g, " ").slice(0, 120)}" trainable="${trainable.replace(/\n/g, " ").slice(0, 160)}"`);
    await attach(testInfo, "train-submit.json", body);
    expect(r.status(), `POST /api/skill-lab/jobs: ${JSON.stringify(body).slice(0, 200)}`).toBe(201);
    await expect(page).toHaveURL(/tab=train&view=detail&id=/, { timeout: 30_000 });
    await evidence(page, "case26-train-detail-running");
    job = await waitForSkillLabJob(page.request, body.id, "case26", 35 * 60 * 1000);
  }
  const seconds = Math.round((Date.now() - t0) / 1000);
  await attach(testInfo, "train-job.json", job);
  ledger(`case26 job ${job.id} ${job.status} after ${seconds}s`);
  if (job.status !== "succeeded") {
    await captureFailure(page.request, testInfo, job, "case26");
    // N10v3-bug3: a run whose every rollout errored must be FAILED with a readable reason (not a green 0.000)
    ledger(`case26 bug3 check: status=${job.status} error="${String(job.error ?? "").slice(0, 300)}"`);
    expect(String(job.error ?? ""), "failed job carries a readable reason").toMatch(/rollouts? .*failed|no workload access token|first error|produced no scorable result|exited/i);
  }
  await page.goto(`/v2/skill-lab?tab=train&view=detail&id=${encodeURIComponent(job.id)}`);
  await expect(page.getByTestId("v2-train-timeline").or(page.getByTestId("v2-skilllab-progress")).first()).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case26-train-detail-final");
  expect(job.status, `train job must succeed: ${job.error ?? ""}`).toBe("succeeded");
  expect(seconds).toBeLessThanOrEqual(30 * 60);

  const summary = await (await page.request.get(`/api/skill-lab/jobs/${job.id}/train-summary`)).json();
  await attach(testInfo, "train-summary.json", summary);
  const steps = (summary.steps ?? summary.history ?? []) as unknown[];
  ledger(`case26 train-summary keys=${Object.keys(summary).join(",")} steps=${steps.length} best=${JSON.stringify(summary.best ?? summary.best_score ?? "").slice(0, 120)}`);
  // a "succeeded" trainer that could not run a single rollout on the worker is not a pass:
  // every `[skilleval] rollout finished: N ok, M errored` line must have N ≥ 1 and M == 0
  const logText = String(((await (await page.request.get(`/api/skill-lab/jobs/${job.id}/log`)).json()) as { content?: string }).content ?? "");
  await attach(testInfo, "train-log.txt", logText);
  const health = rolloutHealth(logText);
  const rolloutOk = health.ok;
  const rolloutErr = health.errored + health.invalid;
  ledger(`case26 rollouts=${JSON.stringify(health.lines)} ok=${rolloutOk} errored=${health.errored} invalid=${health.invalid}`);
  const diff = await (await page.request.get(`/api/skill-lab/jobs/${job.id}/diff`)).json().catch(() => ({}));
  await attach(testInfo, "train-diff.json", diff);
  ledger(`case26 diff changed=${diff.changed}`);
  await expect(page.getByTestId("v2-train-kpis")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("v2-train-curve")).toBeVisible({ timeout: 60_000 });
  const curveSvg = await page.getByTestId("v2-train-curve").locator("svg, canvas, path, circle").count();
  const kpis = (await page.getByTestId("v2-train-kpis").innerText()).replace(/\n/g, " ");
  await evidence(page, "case26-train-curve");
  ledger(`case26 curve drawn elements=${curveSvg} kpis="${kpis.slice(0, 200)}"`);
  expect(steps.length, "≥ 1 recorded training step").toBeGreaterThanOrEqual(1);
  expect(rolloutOk, "≥ 1 rollout actually executed on the worker").toBeGreaterThanOrEqual(1);
  expect(rolloutErr, "no errored rollouts (worker exec failures)").toBe(0);
  expect(curveSvg, "training curve has drawn data").toBeGreaterThan(0);
  expect(kpis).toMatch(/\d/);
  ledger(`case26 end PASS job=${job.id} ${seconds}s steps=${steps.length} changed=${diff.changed}`);
});

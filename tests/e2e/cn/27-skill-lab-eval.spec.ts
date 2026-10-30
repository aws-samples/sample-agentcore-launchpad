import { expect, test } from "@playwright/test";
import { attach, evidence, ledger, registryRecords, rolloutHealth } from "./helpers";
import { captureFailure, latestJob, pickSkillRow, selectV2, SKILL_NAME, TASKSET_NAME, tasksets, waitForSkillLabJob } from "./skilllab-helpers";

/**
 * §4.2 — eval pipeline: Eval wizard (registry skill → task set, `test` split) → asynchronous
 * worker job succeeds ≤ 30 min → results JSON has per-task scores → the results page renders
 * KPIs → the artifacts browser lists the job's files.
 */
test("case 27: eval (test split) → worker job succeeds ≤ 30 min → results page + artifacts render", async ({ page }, testInfo) => {
  test.setTimeout(45 * 60 * 1000);
  const skill = (await registryRecords(page.request)).find((r) => r.name === SKILL_NAME);
  const ts = (await tasksets(page.request)).find((t) => t.name === TASKSET_NAME);
  expect(skill && ts, "case 24 must have created the skill and the task set").toBeTruthy();
  let job = process.env.N10_REUSE ? await latestJob(page.request, "eval", (j) => j.status === "succeeded") : undefined;
  const t0 = Date.now();
  if (!job) {
    await page.goto("/v2/skill-lab?tab=eval&view=new");
    await expect(page.getByTestId("v2-eval-submit")).toBeVisible({ timeout: 60_000 });
    await pickSkillRow(page, "v2-eval-source-skills", skill!.record_id);
    await selectV2(page, "v2-eval-taskset", new RegExp(TASKSET_NAME));
    const testSplit = page.locator(".v2-seg button", { hasText: /^test/ }).first();
    if (await testSplit.isVisible().catch(() => false)) await testSplit.click();
    const judgeMode = await page.getByTestId("v2-eval-judge-mode").innerText().catch(() => "");
    await evidence(page, "case27-eval-wizard");
    const res = page.waitForResponse((r) => r.url().endsWith("/api/skill-lab/jobs") && r.request().method() === "POST", { timeout: 60_000 });
    await page.getByTestId("v2-eval-submit").click();
    const r = await res;
    const body = await r.json().catch(async () => ({ raw: await r.text() }));
    ledger(`case27 submit → ${r.status()} judgeMode="${judgeMode.trim()}" job=${JSON.stringify(body).slice(0, 240)}`);
    await attach(testInfo, "eval-submit.json", body);
    expect(r.status(), `POST /api/skill-lab/jobs: ${JSON.stringify(body).slice(0, 200)}`).toBe(201);
    await expect(page).toHaveURL(/tab=eval&view=detail&id=/, { timeout: 30_000 });
    await evidence(page, "case27-eval-detail-running");
    job = await waitForSkillLabJob(page.request, body.id, "case27", 30 * 60 * 1000);
  }
  const seconds = Math.round((Date.now() - t0) / 1000);
  await attach(testInfo, "eval-job.json", job);
  ledger(`case27 job ${job.id} ${job.status} after ${seconds}s split=${(job.params as { split?: string } | undefined)?.split}`);
  if (job.status !== "succeeded") {
    await captureFailure(page.request, testInfo, job, "case27");
    // N10v3-bug3: a run whose every rollout errored must be FAILED with a readable reason (not a green 0 %)
    ledger(`case27 bug3 check: status=${job.status} error="${String(job.error ?? "").slice(0, 300)}"`);
    const failedResults = await (await page.request.get(`/api/skill-lab/jobs/${job.id}/results`)).json().catch(() => ({}));
    await attach(testInfo, "eval-results-failed.json", failedResults);
    ledger(`case27 bug3 failed-job results summary=${JSON.stringify(failedResults.summary ?? "").slice(0, 200)} rows=${((failedResults.rows ?? []) as unknown[]).length}`);
    expect(String(job.error ?? ""), "failed job carries a readable reason").toMatch(/rollouts? .*failed|no workload access token|first error|nothing was scored|exited/i);
  }
  await page.goto(`/v2/skill-lab?tab=eval&view=detail&id=${encodeURIComponent(job.id)}`);
  await expect(page.getByTestId("v2-eval-kpis").or(page.getByTestId("v2-skilllab-progress")).first()).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case27-eval-detail-final");
  expect(job.status, `eval job must succeed: ${job.error ?? ""}`).toBe("succeeded");
  expect(seconds).toBeLessThanOrEqual(30 * 60);

  const results = await (await page.request.get(`/api/skill-lab/jobs/${job.id}/results`)).json();
  await attach(testInfo, "eval-results.json", results);
  const tasks = (results.rows ?? results.tasks ?? results.results ?? []) as Array<Record<string, unknown>>;
  ledger(`case27 results keys=${Object.keys(results).join(",")} tasks=${tasks.length} summary=${JSON.stringify(results.summary ?? results.pass_rate ?? results.mean_score ?? "").slice(0, 160)}`);
  const errored = tasks.filter((t) => t.error || t.judge_status === "artifact_failure" || t.status === "error").length;
  const firstError = String((tasks.find((t) => t.error) ?? {}).error ?? "").split("\n").filter(Boolean).pop() ?? "";
  if (firstError) ledger(`case27 first task error: ${firstError.slice(0, 240)}`);
  const logText = String(((await (await page.request.get(`/api/skill-lab/jobs/${job.id}/log`)).json()) as { content?: string }).content ?? "");
  await attach(testInfo, "eval-log.txt", logText);
  const health = rolloutHealth(logText);
  ledger(`case27 per-task errors=${errored}/${tasks.length} rollouts=${JSON.stringify(health.lines)} ok=${health.ok} errored=${health.errored} invalid=${health.invalid} summary.errored=${results.summary?.errored} summary.invalid=${results.summary?.invalid}`);
  await expect(page.getByTestId("v2-eval-kpis")).toBeVisible({ timeout: 60_000 });
  const kpis = (await page.getByTestId("v2-eval-kpis").innerText()).replace(/\n/g, " ");
  await evidence(page, "case27-eval-results");
  // artifacts browser
  const artifactsTab = page.locator(".v2-seg button, [role=tab], button", { hasText: /artifacts|产物|文件/i }).first();
  if (await artifactsTab.isVisible().catch(() => false)) await artifactsTab.click();
  const artifacts = await (await page.request.get(`/api/skill-lab/jobs/${job.id}/artifacts`)).json();
  await attach(testInfo, "eval-artifacts.json", artifacts);
  const entries = [...((artifacts.files ?? []) as unknown[]), ...((artifacts.dirs ?? []) as unknown[])];
  const artifactsVisible = await page.getByTestId("v2-skilllab-artifacts").isVisible().catch(() => false);
  await evidence(page, "case27-eval-artifacts");
  ledger(`case27 kpis="${kpis.slice(0, 200)}" artifacts entries=${entries.length} browser visible=${artifactsVisible}`);
  expect(tasks.length, "per-task results").toBeGreaterThanOrEqual(1);
  expect(errored, "no task errored on the worker").toBe(0);
  expect(health.ok, "≥ 1 rollout actually executed on the worker (N10v3-bug2)").toBeGreaterThanOrEqual(1);
  expect(health.errored + health.invalid, "no errored / invalid rollouts").toBe(0);
  expect(typeof results.summary?.errored, "summary.errored present (N10v3-bug3 c)").toBe("number");
  if (Number(results.summary?.errored) > 0) {
    const banner = await page.locator(".v2-alert").filter({ hasText: /errored|artifact failure/i }).first().isVisible().catch(() => false);
    ledger(`case27 errored banner visible=${banner}`);
    expect(banner, "errored-rollouts banner above the KPIs").toBe(true);
  }
  expect(kpis).toMatch(/\d/);
  expect(entries.length, "artifacts listed").toBeGreaterThan(0);
  expect(artifactsVisible, "artifacts browser renders").toBe(true);
  ledger(`case27 end PASS job=${job.id} ${seconds}s tasks=${tasks.length}`);
});

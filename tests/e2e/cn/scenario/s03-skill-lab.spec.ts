import { expect, test, type APIRequestContext } from "@playwright/test";
import { approveRecordViaUi, ledger, registryRecords, rolloutHealth } from "../helpers";
import { captureFailure, ensureSkillRecord, ensureTaskset, pickSkillRow, selectV2, tasksets, waitForSkillLabJob, type SkillLabJob } from "../skilllab-helpers";
import { FALLBACK_SKILL, FALLBACK_TASKSET, gotoRendered, HR_SKILL, record, scnName, snap, stateRequire, stateSet, stepGate } from "./scn";

/**
 * s03 — prepare skills (plan §3 step 3; REQUIREMENTS §4.2 pipelines + §4.1 case 8).
 *
 * The HR skill `expense-report-writer` is approved, then Skill Lab improves it on the OpenCode
 * worker: taskgen (4 HR tasks) → imported as a task set → train 1 epoch → eval → publish the
 * trained SKILL.md back to the Registry (version bump, re-approved). The published, APPROVED
 * record is what the HR assistant mounts in s04.
 *
 * Open question Q1 (plan §8): the generated task set is `single` mode; the proven train path is
 * the seeded `split` pair. When the trainer refuses the generated set, the step falls back to
 * `sample-logtriage-v2` + "Log triage demo tasks" and records `skill.fallback=true`.
 */
test.describe.configure({ mode: "serial" });

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

async function recordVersion(api: APIRequestContext, recordId: string): Promise<{ version: string; status: string }> {
  const rec = (await (await api.get(`/api/registry/records/${recordId}`, { timeout: 60_000 })).json()) as Record<string, unknown>;
  return { version: String(rec.version ?? rec.recordVersion ?? ""), status: String(rec.status) };
}

test("s03a — approve the HR skill; taskgen on the worker ≤ 30 min → review → import as the e2es task set", async ({ page }, testInfo) => {
  stepGate("s03");
  test.setTimeout(45 * 60 * 1000);
  // the seeded record arrives DRAFT / PENDING_APPROVAL; the agent wizard (s04) needs APPROVED
  const skill = await ensureSkillRecord(page, HR_SKILL);
  const before = await recordVersion(page.request, skill.record_id);
  ledger(`s03 skill ${HR_SKILL} record=${skill.record_id} version=${before.version} status=${before.status}`);
  expect(before.status).toBe("APPROVED");
  stateSet("skill", { name: HR_SKILL, record_id: skill.record_id, version_before: before.version, fallback: false, jobs: {} });

  const t0 = Date.now();
  await page.goto("/v2/skill-lab?tab=tasksets&view=gen-new");
  await expect(page.getByTestId("v2-taskgen-submit")).toBeVisible({ timeout: 60_000 });
  await pickSkillRow(page, "v2-taskgen-skills", skill.record_id);
  await page.getByTestId("v2-taskgen-count").fill("4");
  await page
    .getByTestId("v2-taskgen-guidance")
    .fill("Generate expense-report drafting tasks for an HR assistant: each task gives an employee id, a travel period and 2–4 expense lines (category, amount, receipt yes/no) and a rubric that checks the report lists every line, flags missing receipts and states the correct total.");
  const backend = await page.getByTestId("v2-taskgen-backend").innerText();
  const model = await page.getByTestId("v2-taskgen-target-model").inputValue();
  await snap(page, "s03-taskgen-wizard", `${backend.trim()} ${model}`);
  const res = page.waitForResponse((r) => r.url().endsWith("/api/skill-lab/jobs") && r.request().method() === "POST", { timeout: 60_000 });
  await page.getByTestId("v2-taskgen-submit").click();
  const r = await res;
  const body = (await r.json().catch(async () => ({ raw: await r.text() }))) as { id?: string; raw?: string };
  await record(testInfo, "taskgen-submit.json", body, `POST → ${r.status()} backend=${backend.trim()} model=${model}`);
  expect(r.status(), `POST /api/skill-lab/jobs: ${JSON.stringify(body).slice(0, 200)}`).toBe(201);
  const job = await waitForSkillLabJob(page.request, body.id!, "s03 taskgen", 30 * 60 * 1000);
  const seconds = Math.round((Date.now() - t0) / 1000);
  await record(testInfo, "taskgen-job.json", job, `${job.status} after ${seconds}s`);
  if (job.status !== "succeeded") await captureFailure(page.request, testInfo, job, "s03 taskgen");
  await page.goto(`/v2/skill-lab?tab=tasksets&view=gen&id=${encodeURIComponent(job.id)}`);
  await expect(page.getByTestId("v2-taskgen-log").or(page.getByTestId("v2-taskgen-review")).first()).toBeVisible({ timeout: 60_000 });
  await snap(page, "s03-taskgen-final", job.status);
  expect(job.status, `taskgen must succeed: ${job.error ?? ""}`).toBe("succeeded");
  expect(seconds, "taskgen ≤ 30 min").toBeLessThanOrEqual(30 * 60);

  const results = (await (await page.request.get(`/api/skill-lab/jobs/${job.id}/results`, { timeout: 60_000 })).json()) as { tasks?: unknown[] };
  expect((results.tasks ?? []).length, "≥ 1 generated task").toBeGreaterThanOrEqual(1);
  await expect(page.getByTestId("v2-taskgen-review")).toBeVisible({ timeout: 60_000 });
  const tsName = scnName("expense-tasks");
  await page.getByTestId("v2-taskgen-import-name").fill(tsName);
  const imp = page.waitForResponse((rr) => rr.url().includes("/import-taskset") && rr.request().method() === "POST", { timeout: 60_000 });
  await page.getByTestId("v2-taskgen-import").click();
  const ir = await imp;
  const ib = await ir.json().catch(async () => ({ raw: await ir.text() }));
  await record(testInfo, "taskgen-import.json", ib, `import-taskset → ${ir.status()}`);
  expect(ir.status()).toBe(201);
  const row = (await tasksets(page.request)).find((t) => t.name === tsName);
  expect(row, "imported task set listed").toBeTruthy();
  await snap(page, "s03-taskset-imported", `${tsName} mode=${row!.mode}`);
  stateSet("skill", { ...stateRequire("skill", "s03"), taskset_id: row!.id, jobs: { taskgen: job.id } });
  ledger(`s03a PASS taskgen=${job.id} ${seconds}s tasks=${(results.tasks ?? []).length} taskset=${row!.id} (${row!.mode})`);
});

test("s03b — train 1 epoch on the worker ≤ 30 min (generated task set; seeded pair as the recorded fallback) → curve with data", async ({ page }, testInfo) => {
  stepGate("s03");
  test.setTimeout(55 * 60 * 1000);
  const st = stateRequire("skill", "s03");
  const generated = (await tasksets(page.request)).find((t) => t.id === st.taskset_id);
  expect(generated, "the generated task set from s03a").toBeTruthy();

  const submitTrain = async (recordId: string, tasksetName: string): Promise<{ status: number; body: { id?: string } }> => {
    await page.goto("/v2/skill-lab?tab=train&view=new");
    await expect(page.getByTestId("v2-train-submit")).toBeVisible({ timeout: 60_000 });
    await pickSkillRow(page, "v2-train-source-skills", recordId);
    await selectV2(page, "v2-train-taskset", new RegExp(escapeRe(tasksetName)));
    // SCENARIO_TRAIN_EPOCHS (default 1): more epochs raise the chance of an accepted edit for the publish step
    const epochs = page.getByTestId("v2-train-epochs");
    if (await epochs.isVisible().catch(() => false)) await epochs.fill(process.env.SCENARIO_TRAIN_EPOCHS ?? "1");
    await snap(page, "s03-train-wizard", tasksetName);
    const res = page.waitForResponse((r) => r.url().endsWith("/api/skill-lab/jobs") && r.request().method() === "POST", { timeout: 60_000 }).catch(() => null);
    await page.getByTestId("v2-train-submit").click();
    const r = await res;
    if (!r) {
      const alert = (await page.getByRole("alert").first().innerText().catch(() => "")).replace(/\s+/g, " ").trim();
      ledger(`s03 train submit on "${tasksetName}": no POST observed; alert="${alert.slice(0, 200)}"`);
      return { status: 0, body: {} };
    }
    const body = (await r.json().catch(async () => ({ raw: await r.text() }))) as { id?: string };
    ledger(`s03 train submit on "${tasksetName}" → ${r.status()} ${JSON.stringify(body).slice(0, 200)}`);
    return { status: r.status(), body };
  };

  const t0 = Date.now();
  let skillName = st.name;
  let recordId = st.record_id;
  let fallback = false;
  let sub = await submitTrain(recordId, generated!.name);
  if (sub.status !== 201) {
    // Q1: the trainer did not take the generated (single-mode) set — proven seeded split pair instead
    fallback = true;
    testInfo.annotations.push({ type: "deviation", description: `trainer refused the generated task set (${sub.status}); fell back to ${FALLBACK_SKILL} + "${FALLBACK_TASKSET}" (plan Q1)` });
    const fb = await ensureSkillRecord(page, FALLBACK_SKILL);
    // cold start N11-r5-bug3: the sample task sets may not be seeded — upload the demo split pair through the console editor
    const ts = await ensureTaskset(page, FALLBACK_TASKSET);
    ledger(`s03 fallback task set "${FALLBACK_TASKSET}" ${ts.row.id} created=${ts.created} via=${ts.via}`);
    skillName = FALLBACK_SKILL;
    recordId = fb.record_id;
    const fbBefore = (await (await page.request.get(`/api/registry/records/${recordId}`, { timeout: 60_000 })).json()) as Record<string, unknown>;
    stateSet("skill", { ...st, name: skillName, record_id: recordId, version_before: String(fbBefore.version ?? ""), fallback: true });
    sub = await submitTrain(recordId, FALLBACK_TASKSET);
  }
  expect(sub.status, "train job accepted").toBe(201);
  await expect(page).toHaveURL(/tab=train&view=detail&id=/, { timeout: 30_000 });
  const job: SkillLabJob = await waitForSkillLabJob(page.request, sub.body.id!, "s03 train", 35 * 60 * 1000);
  const seconds = Math.round((Date.now() - t0) / 1000);
  await record(testInfo, "train-job.json", job, `${job.status} after ${seconds}s fallback=${fallback}`);
  if (job.status !== "succeeded") await captureFailure(page.request, testInfo, job, "s03 train");
  await page.goto(`/v2/skill-lab?tab=train&view=detail&id=${encodeURIComponent(job.id)}`);
  await expect(page.getByTestId("v2-train-timeline").or(page.getByTestId("v2-skilllab-progress")).first()).toBeVisible({ timeout: 60_000 });
  await snap(page, "s03-train-final", job.status);
  expect(job.status, `train must succeed: ${job.error ?? ""}`).toBe("succeeded");
  expect(seconds, "train ≤ 30 min").toBeLessThanOrEqual(30 * 60);

  const summary = (await (await page.request.get(`/api/skill-lab/jobs/${job.id}/train-summary`, { timeout: 60_000 })).json()) as Record<string, unknown>;
  const steps = ((summary.steps ?? summary.history ?? []) as unknown[]).length;
  const logText = String(((await (await page.request.get(`/api/skill-lab/jobs/${job.id}/log`, { timeout: 60_000 })).json()) as { content?: string }).content ?? "");
  const health = rolloutHealth(logText);
  const diff = (await (await page.request.get(`/api/skill-lab/jobs/${job.id}/diff`, { timeout: 60_000 })).json().catch(() => ({}))) as { changed?: boolean };
  await record(testInfo, "train-summary.json", { summary, health, diff }, `steps=${steps} rollouts ok=${health.ok} errored=${health.errored} invalid=${health.invalid} changed=${diff.changed}`);
  await expect(page.getByTestId("v2-train-kpis")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("v2-train-curve")).toBeVisible({ timeout: 60_000 });
  const drawn = await page.getByTestId("v2-train-curve").locator("svg, canvas, path, circle").count();
  await snap(page, "s03-train-curve", `steps=${steps}`, { key: true });
  expect(steps, "≥ 1 recorded training step").toBeGreaterThanOrEqual(1);
  expect(health.ok, "≥ 1 rollout executed on the worker").toBeGreaterThanOrEqual(1);
  expect(health.errored + health.invalid, "no errored / invalid rollouts").toBe(0);
  expect(drawn, "training curve drawn").toBeGreaterThan(0);
  const trainJob = job.id;
  const changed = diff.changed === true;
  if (!fallback && !changed) {
    // §6 amendment (2026-10-12): the trainer found no SKILL.md that beats the current one — a legitimate outcome when
    // the best validation score is not below the seed baseline; annotated `no-improvement`, the skill is mounted as is
    const best = Number(summary.best_score ?? (summary.best as Record<string, unknown> | undefined)?.score ?? NaN);
    const seed = Number(summary.seed_baseline ?? summary.baseline ?? NaN);
    const notWorse = !Number.isFinite(best) || !Number.isFinite(seed) || best >= seed;
    ledger(`s03b no accepted edit: best=${best} seed_baseline=${seed} not_worse=${notWorse} (summary keys ${Object.keys(summary).join(",")})`);
    expect(notWorse, "trainer result not worse than the seed baseline").toBe(true);
    testInfo.annotations.push({ type: "no-improvement", description: `train ${job.id} (${process.env.SCENARIO_TRAIN_EPOCHS ?? "1"} epoch(s)) found no SKILL.md beating the current ${st.name}; best=${best} seed=${seed}` });
  }
  const cur = stateRequire("skill", "s03");
  stateSet("skill", { ...cur, fallback, jobs: { ...cur.jobs, train: trainJob } });
  ledger(`s03b PASS train=${trainJob} ${seconds}s skill=${skillName} fallback=${fallback} changed=${changed}`);
});

test("s03c — eval on the worker ≤ 30 min → results; publish the trained skill → Registry version bump, APPROVED", async ({ page }, testInfo) => {
  stepGate("s03");
  test.setTimeout(50 * 60 * 1000);
  const st = stateRequire("skill", "s03");
  const tasksetName = st.fallback ? FALLBACK_TASKSET : (await tasksets(page.request)).find((t) => t.id === st.taskset_id)!.name;

  const t0 = Date.now();
  await page.goto("/v2/skill-lab?tab=eval&view=new");
  await expect(page.getByTestId("v2-eval-submit")).toBeVisible({ timeout: 60_000 });
  await pickSkillRow(page, "v2-eval-source-skills", st.record_id);
  await selectV2(page, "v2-eval-taskset", new RegExp(escapeRe(tasksetName)));
  const testSplit = page.locator(".v2-seg button", { hasText: /^test/ }).first();
  if (st.fallback && (await testSplit.isVisible().catch(() => false))) await testSplit.click();
  await snap(page, "s03-eval-wizard", tasksetName);
  const res = page.waitForResponse((r) => r.url().endsWith("/api/skill-lab/jobs") && r.request().method() === "POST", { timeout: 60_000 });
  await page.getByTestId("v2-eval-submit").click();
  const r = await res;
  const body = (await r.json().catch(async () => ({ raw: await r.text() }))) as { id?: string };
  expect(r.status(), `POST /api/skill-lab/jobs: ${JSON.stringify(body).slice(0, 200)}`).toBe(201);
  const job = await waitForSkillLabJob(page.request, body.id!, "s03 eval", 30 * 60 * 1000);
  const seconds = Math.round((Date.now() - t0) / 1000);
  await record(testInfo, "eval-job.json", job, `${job.status} after ${seconds}s`);
  if (job.status !== "succeeded") await captureFailure(page.request, testInfo, job, "s03 eval");
  await page.goto(`/v2/skill-lab?tab=eval&view=detail&id=${encodeURIComponent(job.id)}`);
  await expect(page.getByTestId("v2-eval-kpis").or(page.getByTestId("v2-skilllab-progress")).first()).toBeVisible({ timeout: 60_000 });
  await snap(page, "s03-eval-final", job.status);
  expect(job.status, `eval must succeed: ${job.error ?? ""}`).toBe("succeeded");
  expect(seconds, "eval ≤ 30 min").toBeLessThanOrEqual(30 * 60);
  const results = (await (await page.request.get(`/api/skill-lab/jobs/${job.id}/results`, { timeout: 60_000 })).json()) as { rows?: Array<Record<string, unknown>>; tasks?: Array<Record<string, unknown>>; summary?: { errored?: number } };
  const rows = results.rows ?? results.tasks ?? [];
  await record(testInfo, "eval-results.json", results, `rows=${rows.length} summary.errored=${results.summary?.errored}`);
  expect(rows.length, "per-task results").toBeGreaterThanOrEqual(1);
  expect(Number(results.summary?.errored ?? 0), "no task errored on the worker").toBe(0);
  const artifacts = (await (await page.request.get(`/api/skill-lab/jobs/${job.id}/artifacts`, { timeout: 60_000 })).json()) as { files?: unknown[]; dirs?: unknown[] };
  expect([...(artifacts.files ?? []), ...(artifacts.dirs ?? [])].length, "artifacts listed").toBeGreaterThan(0);

  // publish the trained SKILL.md back to the Registry
  const train = st.jobs.train!;
  const diff = (await (await page.request.get(`/api/skill-lab/jobs/${train}/diff`, { timeout: 60_000 })).json().catch(() => ({}))) as { changed?: boolean };
  await page.goto(`/v2/skill-lab?tab=train&view=detail&id=${encodeURIComponent(train)}`);
  await expect(page.getByTestId("v2-train-publish")).toBeVisible({ timeout: 60_000 });
  let publishStatus = -1;
  const btn = page.getByTestId("v2-train-publish-btn");
  if ((await btn.isVisible().catch(() => false)) && (await btn.isEnabled())) {
    await btn.click();
    await expect(page.getByTestId("v2-train-publish-dialog")).toBeVisible({ timeout: 10_000 });
    const re = page.getByTestId("v2-train-reapprove");
    if ((await re.isVisible().catch(() => false)) && !(await re.isChecked())) await re.check();
    await snap(page, "s03-publish-dialog");
    const pub = page.waitForResponse((rr) => rr.url().includes("/publish") && rr.request().method() === "POST", { timeout: 60_000 });
    await page.getByTestId("v2-train-publish-confirm").click();
    const pr = await pub;
    publishStatus = pr.status();
    await record(testInfo, "publish.json", await pr.json().catch(async () => ({ raw: await pr.text() })), `publish → ${publishStatus}`);
  } else {
    testInfo.annotations.push({ type: "no-improvement", description: `publish not offered (diff.changed=${diff.changed}) — the trainer produced no accepted edit (§6 amendment 2026-10-12); the skill is mounted at its current version` });
    ledger(`s03 publish not offered (diff.changed=${diff.changed})`);
  }
  let rec = (await (await page.request.get(`/api/registry/records/${st.record_id}`, { timeout: 60_000 })).json()) as Record<string, unknown>;
  if (rec.status !== "APPROVED") {
    ledger(`s03 record ${rec.status} after publish — re-approving through the Registry UI`);
    await approveRecordViaUi(page, st.record_id);
    rec = (await (await page.request.get(`/api/registry/records/${st.record_id}`, { timeout: 60_000 })).json()) as Record<string, unknown>;
  }
  const versionAfter = String(rec.version ?? rec.recordVersion ?? "");
  await gotoRendered(page, `/v2/registry?view=detail&id=${encodeURIComponent(st.record_id)}`, String(rec.name ?? st.name ?? HR_SKILL)); // N-SCN-obs19
  await snap(page, "s03-registry-after-publish", `${st.version_before} → ${versionAfter} ${rec.status}`, { key: true });
  expect(rec.status, "skill record APPROVED for the agent wizard").toBe("APPROVED");
  if (publishStatus >= 0) {
    expect(publishStatus, "publish → 200").toBe(200);
    expect(versionAfter, "registry version bumped").not.toBe(st.version_before);
  } else {
    // a trainer that finds no better SKILL.md is a legitimate outcome (the seed scores well): recorded as a
    // deviation → the storyline run is PARTIAL (lead ruling Q7 pattern), not failed; the agent mounts the current version
    ledger(`s03 no accepted edit (diff.changed=${diff.changed}) — publish skipped, version stays ${st.version_before}`);
  }
  const approved = (await registryRecords(page.request)).find((x) => x.record_id === st.record_id);
  expect(approved?.status).toBe("APPROVED");
  stateSet("skill", { ...st, version_after: versionAfter, jobs: { ...st.jobs, eval: job.id } });
  ledger(`s03c PASS eval=${job.id} ${seconds}s publish=${publishStatus} version ${st.version_before} → ${versionAfter}`);
});

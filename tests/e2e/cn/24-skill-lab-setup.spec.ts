import { expect, test } from "@playwright/test";
import { attach, evidence, ledger, registryRecords, stateSet } from "./helpers";
import { ensureSkillRecord, ensureTaskset, FALLBACK_SKILL, FALLBACK_TASKSET, SAMPLE_SKILL, SAMPLE_TASKSET, tasksets } from "./skilllab-helpers";

/**
 * §4.2 setup (round 4, N10v3-bug1 re-check) — the demo material from
 * `vendor/skillopt/data/skilleval_demo` must be seeded by the product on the hub
 * (`bootstrap.ensure_skill_lab` → `ensure_skill_lab_samples`): skills `sample-logtriage`,
 * `sample-logtriage-v2`, `sample-report` in the Registry and three task sets flagged
 * `sample`. Case 24 asserts that WITHOUT importing anything; the pipeline specs 25–28 then
 * run on `sample-logtriage` + "Log triage demo tasks". Only if the samples are missing does
 * the spec fall back to the round-3 console import (recorded as the bug1 residue) so the
 * pipeline cases can still run.
 */
test("case 24: demo samples seeded by the product (3 sample-* skills, 3 sample task sets) → sample-logtriage approved for the pipelines", async ({ page }, testInfo) => {
  const recordsBefore = await registryRecords(page.request);
  const sampleSkills = recordsBefore.filter((r) => r.name.startsWith("sample-"));
  const tsBefore = await tasksets(page.request);
  const sampleTasksets = tsBefore.filter((t) => t.sample);
  await attach(testInfo, "samples-before.json", { skills: sampleSkills, tasksets: tsBefore });
  ledger(`case24 seeded samples: skills=${sampleSkills.map((s) => `${s.name}:${s.status}`).join(",") || "NONE"} tasksets=${sampleTasksets.map((t) => `${t.name}(${t.mode})`).join(",") || "NONE"} (all tasksets=${tsBefore.length})`);
  await page.goto("/v2/skill-lab?tab=tasksets");
  await expect(page.getByTestId("v2-taskset-table")).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case24-tasksets-list-seeded");

  const seeded = sampleSkills.some((s) => s.name === SAMPLE_SKILL) && sampleTasksets.some((t) => t.name === SAMPLE_TASKSET);
  const skillName = seeded ? SAMPLE_SKILL : FALLBACK_SKILL;
  const tasksetName = seeded ? SAMPLE_TASKSET : FALLBACK_TASKSET;
  stateSet("sl_skill", skillName);
  stateSet("sl_taskset", tasksetName);
  stateSet("sl_seeded", seeded);
  if (!seeded) ledger(`case24 N10v3-bug1 STILL OPEN on this env — falling back to the console import ${skillName} / ${tasksetName}`);

  // the seeded AGENT_SKILLS records arrive as DRAFT (the Skill Lab flow mounts them regardless);
  // case 28 mounts the skill on a Harness agent through the wizard, which needs APPROVED — approve it in the Registry UI
  const skill = await ensureSkillRecord(page, skillName);
  await page.goto(`/v2/registry?view=detail&id=${encodeURIComponent(skill.record_id)}`);
  await expect(page.getByText(skillName).first()).toBeVisible({ timeout: 30_000 });
  await evidence(page, "case24-skill-approved");
  ledger(`case24 skill ${skillName} record=${skill.record_id} status=${skill.status} created=${skill.created}`);
  expect(skill.status).toBe("APPROVED");
  expect(skill.created, "the pipeline skill was seeded by the product, not imported by the test").toBe(false);

  const ts = await ensureTaskset(page, tasksetName);
  const full = await (await page.request.get(`/api/skill-lab/tasksets/${ts.row.id}?full=true`)).json();
  await attach(testInfo, "taskset.json", full);
  const counts = Object.fromEntries(Object.entries((full.tasks_by_split ?? {}) as Record<string, unknown[]>).map(([k, v]) => [k, v.length]));
  ledger(`case24 taskset ${tasksetName} id=${ts.row.id} mode=${full.info?.mode ?? full.mode} sample=${ts.row.sample} counts=${JSON.stringify(counts)} created=${ts.created} via=${ts.via}`);
  await page.goto(`/v2/skill-lab?tab=tasksets&view=detail&id=${encodeURIComponent(ts.row.id)}`);
  await expect(page.getByText(tasksetName).first()).toBeVisible({ timeout: 30_000 });
  await evidence(page, "case24-taskset-detail");
  expect(String(full.info?.mode ?? full.mode)).toBe("split");
  expect(counts).toEqual({ train: 4, val: 3, test: 3 });
  expect(ts.created, "the pipeline task set was seeded by the product").toBe(false);
  expect(seeded, "sample-logtriage + 'Log triage demo tasks' seeded (N10v3-bug1)").toBe(true);
});

test("case 24b: the vendored demo samples are seeded by the product (3 sample-* skills, 3 sample task sets)", async ({ page }, testInfo) => {
  const sampleSkills = (await registryRecords(page.request)).filter((r) => r.name.startsWith("sample-"));
  const sampleTasksets = (await tasksets(page.request)).filter((t) => t.sample);
  await attach(testInfo, "samples.json", { skills: sampleSkills, tasksets: sampleTasksets });
  ledger(`case24b sample skills=${sampleSkills.length} [${sampleSkills.map((s) => s.name).join(",")}] sample tasksets=${sampleTasksets.length} [${sampleTasksets.map((t) => t.name).join(",")}]`);
  const names = new Set(sampleSkills.map((s) => s.name));
  expect(names, "sample-logtriage / sample-logtriage-v2 / sample-report registered").toEqual(new Set(["sample-logtriage", "sample-logtriage-v2", "sample-report"]));
  expect(new Set(sampleTasksets.map((t) => t.name)), "3 sample task sets").toEqual(new Set(["Log triage demo tasks", "CSV report demo tasks", "Excel handling demo tasks"]));
});

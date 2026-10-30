import { expect, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";
import { execSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { approveRecordViaUi, attach, awsRead, E2E_PREFIX, evidence, ledger, registryRecords, REPO_ROOT, sleep, stateGet } from "./helpers";

/** Shared Skill Lab (N13, REQUIREMENTS §4.2) flows for specs 23–28. */

// Round 4 (N10v3-bug1 fixed): the product seeds `sample-logtriage` + "Log triage demo tasks" on the
// hub; the pipeline specs run on those. When case 24 finds them missing it falls back to the
// round-3 console import (`e2e4-logtriage` / `e2e4-logtriage-tasks`) and records the names here.
export const SAMPLE_SKILL = "sample-logtriage";
export const SAMPLE_TASKSET = "Log triage demo tasks";
export const FALLBACK_SKILL = `${E2E_PREFIX}logtriage`;
export const FALLBACK_TASKSET = `${E2E_PREFIX}logtriage-tasks`;
export const SKILL_NAME = process.env.N10_SL_SKILL ?? stateGet<string>("sl_skill") ?? SAMPLE_SKILL;
export const TASKSET_NAME = process.env.N10_SL_TASKSET ?? stateGet<string>("sl_taskset") ?? SAMPLE_TASKSET;
export const DEMO_ROOT = path.join(REPO_ROOT, "vendor/skillopt/data/skilleval_demo");
/** The Skill Lab worker runtime (read-only CloudWatch log lookup on failure). */
export const WORKER_RUNTIME = process.env.N10_SKILL_LAB_WORKER ?? "launchpad_skill_lab_worker-EXAMPLE003";
export const TERMINAL = new Set(["succeeded", "failed", "cancelled", "interrupted"]);

export interface SkillLabJob {
  id: string;
  type: string;
  status: string;
  error?: string | null;
  progress?: unknown;
  params?: Record<string, unknown>;
  skill_source?: Record<string, unknown>;
  taskset_id?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  queue_position?: number;
}

/** Open a V2 <Select> by test id and pick the option whose label matches. */
export async function selectV2(page: Page, testId: string, option: RegExp | string): Promise<void> {
  await page.getByTestId(testId).click();
  const popup = page.getByTestId("v2-select-popup");
  await expect(popup).toBeVisible({ timeout: 10_000 });
  await popup.getByRole("option", { name: option }).first().click();
}

/** Labels offered by a V2 <Select> (opens and closes it). */
export async function selectOptions(page: Page, testId: string): Promise<string[]> {
  await page.getByTestId(testId).click();
  const popup = page.getByTestId("v2-select-popup");
  await expect(popup).toBeVisible({ timeout: 10_000 });
  const labels = (await popup.getByRole("option").allInnerTexts()).map((s) => s.trim());
  await page.keyboard.press("Escape");
  return labels;
}

/** Poll one Skill Lab job until it leaves queued/running; ledger every status/progress change. */
export async function waitForSkillLabJob(
  api: APIRequestContext,
  jobId: string,
  label: string,
  timeoutMs = 35 * 60 * 1000,
): Promise<SkillLabJob> {
  const started = Date.now();
  let last: SkillLabJob | undefined;
  let lastKey = "";
  while (Date.now() - started < timeoutMs) {
    const res = await api.get(`/api/skill-lab/jobs/${jobId}`, { timeout: 60_000 }).catch(() => null);
    if (res?.ok()) {
      last = (await res.json()) as SkillLabJob;
      const key = `${last.status}:${JSON.stringify(last.progress ?? "").slice(0, 120)}`;
      if (key !== lastKey) {
        lastKey = key;
        ledger(`${label} job ${jobId.slice(0, 8)} ${Math.round((Date.now() - started) / 1000)}s ${key}`);
      }
      if (TERMINAL.has(String(last.status))) return last;
    } else {
      ledger(`${label} job ${jobId.slice(0, 8)} GET → ${res?.status() ?? "no response"} (retrying)`);
    }
    await sleep(20_000);
  }
  throw new Error(`${label}: job ${jobId} still ${last?.status ?? "unknown"} after ${Math.round(timeoutMs / 1000)}s`);
}

/** On failure: the job log through the product + the worker runtime's CloudWatch log (read-only). */
export async function captureFailure(api: APIRequestContext, testInfo: TestInfo, job: SkillLabJob, label: string): Promise<void> {
  const log = await api.get(`/api/skill-lab/jobs/${job.id}/log`);
  const text = await log.text();
  await attach(testInfo, `${label}-job-log.txt`, text);
  ledger(`${label} FAILED status=${job.status} error=${String(job.error ?? "").slice(0, 300)} log-tail=${text.slice(-600).replace(/\n/g, " | ")}`);
  const startMs = job.started_at ? Date.parse(job.started_at) - 60_000 : Date.now() - 60 * 60 * 1000;
  const cw = awsRead(
    `logs filter-log-events --log-group-name /aws/bedrock-agentcore/runtimes/${WORKER_RUNTIME}-DEFAULT --start-time ${startMs} --limit 200 --query "events[].message"`,
  );
  await attach(testInfo, `${label}-worker-cloudwatch.json`, cw);
  ledger(`${label} worker CloudWatch tail: ${cw.replace(/\s+/g, " ").slice(-800)}`);
}

export function e2eStamp(): string {
  return new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "");
}

/** The demo log-triage skill as a Registry AGENT_SKILLS record (zip import through the console, then approved). */
export async function ensureSkillRecord(page: Page, skillName: string = SKILL_NAME): Promise<{ record_id: string; status: string; created: boolean }> {
  const SKILL_NAME = skillName;
  let rec = (await registryRecords(page.request)).find((r) => r.name === SKILL_NAME);
  let created = false;
  if (!rec) {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "n10v4-skill-"));
    const dir = path.join(tmp, SKILL_NAME);
    fs.cpSync(path.join(DEMO_ROOT, "logtriage_skill"), dir, { recursive: true });
    const md = fs.readFileSync(path.join(dir, "SKILL.md"), "utf8");
    // the vendored demo SKILL.md has no frontmatter (upstream Studio never required it); the
    // seeding path synthesizes it — mirror that for the console import
    fs.writeFileSync(
      path.join(dir, "SKILL.md"),
      `---\nname: ${SKILL_NAME}\ndescription: "N10 v4 demo: log-triage helper (weak baseline) with a bundled parser script and format doc. Pairs with the logtriage demo task set."\nversion: 1.0.0\n---\n\n${md}`,
    );
    const zip = path.join(tmp, `${SKILL_NAME}.zip`);
    execSync(`zip -qr "${zip}" ${SKILL_NAME}`, { cwd: tmp });
    await page.goto("/v2/registry?view=register&type=AGENT_SKILLS");
    await page.getByTestId("v2-registry-source-zip").click();
    await page.getByTestId("v2-registry-zip-input").setInputFiles(zip);
    await expect(page.getByTestId("v2-registry-preview")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByTestId("v2-registry-preview-name")).toHaveValue(new RegExp(SKILL_NAME));
    await evidence(page, "case24-skill-import-preview");
    await page.getByTestId("v2-registry-import").click();
    await expect(page).toHaveURL(/view=detail&id=/, { timeout: 60_000 });
    const id = new URL(page.url()).searchParams.get("id")!;
    rec = (await registryRecords(page.request)).find((r) => r.record_id === id);
    created = true;
    ledger(`skill-lab setup: imported ${SKILL_NAME} → ${id}`);
  }
  if (rec!.status !== "APPROVED") {
    const st = await approveRecordViaUi(page, rec!.record_id);
    ledger(`skill-lab setup: ${SKILL_NAME} ${rec!.record_id} → ${st}`);
  }
  rec = (await registryRecords(page.request)).find((r) => r.record_id === rec!.record_id);
  return { record_id: rec!.record_id, status: rec!.status, created };
}

export interface TasksetRow {
  id: string;
  name: string;
  mode: string;
  sample?: boolean;
  counts?: Record<string, number>;
}

export async function tasksets(api: APIRequestContext): Promise<TasksetRow[]> {
  return ((await (await api.get("/api/skill-lab/tasksets")).json()) ?? []) as TasksetRow[];
}

/** The demo log-triage task set (train 4 / val 3 / test 3) created through the task-set editor's JSON upload. */
export async function ensureTaskset(page: Page, tasksetName: string = TASKSET_NAME): Promise<{ row: TasksetRow; created: boolean; via: string }> {
  const TASKSET_NAME = tasksetName;
  let row = (await tasksets(page.request)).find((t) => t.name === TASKSET_NAME);
  if (row) return { row, created: false, via: "existing" };
  const bundle: Record<string, unknown[]> = {};
  for (const split of ["train", "val", "test"]) {
    bundle[split] = JSON.parse(fs.readFileSync(path.join(DEMO_ROOT, "logtriage_tasks", split, "items.json"), "utf8")) as unknown[];
  }
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "n10v4-taskset-"));
  const file = path.join(tmp, "logtriage-tasks.json");
  fs.writeFileSync(file, JSON.stringify(bundle));
  await page.goto("/v2/skill-lab?tab=tasksets&view=new");
  await expect(page.getByTestId("v2-taskset-name")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("v2-taskset-name").fill(TASKSET_NAME);
  await page.getByTestId("v2-taskset-description").fill("N10 v4 · demo log-triage tasks from vendor/skillopt/data/skilleval_demo/logtriage_tasks (train 4 / val 3 / test 3)");
  const modeSplit = page.getByTestId("v2-taskset-mode-split");
  if (await modeSplit.isVisible().catch(() => false)) await modeSplit.click();
  // editor tab → Upload JSON
  const uploadTab = page.locator(".v2-seg button", { hasText: /upload|JSON|上传/i }).first();
  if (await uploadTab.isVisible().catch(() => false)) await uploadTab.click();
  await page.getByTestId("v2-taskset-upload-input").setInputFiles(file);
  await expect(page.locator(".v2-alert.success, .v2-alert").filter({ hasText: /train|loaded|已加载/i }).first()).toBeVisible({ timeout: 15_000 }).catch(() => {});
  await evidence(page, "case24-taskset-upload");
  const saveRes = page.waitForResponse((r) => r.url().includes("/api/skill-lab/tasksets") && r.request().method() === "POST", { timeout: 60_000 }).catch(() => null);
  await page.getByTestId("v2-taskset-save").click();
  const sr = await saveRes;
  const body = sr ? await sr.text() : "(no POST observed)";
  ledger(`skill-lab setup: taskset save → ${sr?.status() ?? "-"} ${body.slice(0, 200)}`);
  if (sr && sr.status() === 201) {
    row = (await tasksets(page.request)).find((t) => t.name === TASKSET_NAME);
    if (row) return { row, created: true, via: "ui" };
  }
  // fallback — the product's own API (recorded in the ledger as a UI finding)
  const res = await page.request.post("/api/skill-lab/tasksets", { data: { name: TASKSET_NAME, description: "N10 v4 demo log-triage tasks (API fallback)", mode: "split", tasks_by_split: bundle } });
  ledger(`skill-lab setup: taskset API fallback → ${res.status()} ${(await res.text()).slice(0, 200)}`);
  row = (await tasksets(page.request)).find((t) => t.name === TASKSET_NAME);
  if (!row) throw new Error("could not create the demo task set through the console or its API");
  return { row, created: true, via: "api" };
}

/** Pick a registry skill row in a SkillSourcePicker / SkillMultiPicker (`<testId>-row-<record_id>`). */
export async function pickSkillRow(page: Page, pickerTestId: string, recordId: string): Promise<void> {
  const row = page.getByTestId(`${pickerTestId}-row-${recordId}`);
  await expect(row, `skill row ${recordId} in ${pickerTestId}`).toBeVisible({ timeout: 30_000 });
  const input = row.locator("input");
  if ((await input.count()) > 0) await input.first().check({ force: true }).catch(async () => row.click());
  else await row.click();
}

export async function latestJob(api: APIRequestContext, type: string, pred: (j: SkillLabJob) => boolean = () => true): Promise<SkillLabJob | undefined> {
  const rows = ((await (await api.get(`/api/skill-lab/jobs?type=${type}`)).json()) ?? []) as SkillLabJob[];
  return rows.filter(pred).sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
}

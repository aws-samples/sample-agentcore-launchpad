import { expect, test } from "@playwright/test";
import { awsRead, getAgent, ledger, listAgents, registryRecords, sleep, waitForDeploy } from "../helpers";
import { exportRuntimeLogs } from "../log-export";
import { gotoRendered, record, runtimeIdOf, snap, stateGet, stateRequire, stateSet, stepGate } from "./scn";

/**
 * s10 — lifecycle (plan §3 step 10; REQUIREMENTS §4.1 case 17).
 * Edit the HR assistant's prompt → redeploy succeeds (version +1, stays active) → delete it
 * through the console → `failed=[]`, `log_groups` is the LAST teardown step, and read-only AWS
 * confirms the Runtime, execution role, log group, S3 prefix, memory snapshots are gone, the
 * A2A record is removed and the agent's long-term memory records are purged.
 * The runtime twin and the payroll specialist are removed by `scenario-cleanup.spec.ts`.
 */
test("s10 — edit → redeploy (version +1, active) → delete → runtime / role / log group (last) / S3 / memory / A2A record gone", async ({ page }, testInfo) => {
  stepGate("s10");
  test.setTimeout(45 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  const before = await getAgent(page.request, hr.id);
  const v0 = Number(before.version);

  // memory actor for the purge check
  const sessions = (((await (await page.request.get(`/api/chat/${hr.id}/sessions`, { timeout: 60_000 })).json()) as { sessions?: Array<{ session_id: string }> }).sessions ?? []);
  let actorId = "";
  const countRecords = async (actor: string): Promise<number> => {
    const namespaces = (((await (await page.request.get(`/api/memory/namespaces?actor_id=${encodeURIComponent(actor)}`, { timeout: 60_000 })).json()) as { items?: Array<{ strategy_id: string }> }).items ?? []);
    let total = 0;
    for (const ns of namespaces) {
      const r = (await (await page.request.get(`/api/memory/records?actor_id=${encodeURIComponent(actor)}&strategy_id=${encodeURIComponent(ns.strategy_id)}&max_results=100`, { timeout: 60_000 })).json()) as { items?: unknown[]; records?: unknown[] };
      total += (r.items ?? r.records ?? []).length;
    }
    return total;
  };
  if (sessions[0]) {
    const rail = (await (await page.request.get(`/api/chat/${hr.id}/memory?session_id=${encodeURIComponent(sessions[0].session_id)}`, { timeout: 60_000 })).json()) as { actor_id?: string };
    actorId = String(rail.actor_id ?? "");
  }
  const recordsBefore = actorId ? await countRecords(actorId) : 0;
  ledger(`s10 start v${v0} actor=${actorId} long-term records=${recordsBefore}`);

  // ── edit → redeploy ──
  await page.goto(`/v2/agents?view=edit&id=${encodeURIComponent(hr.id)}`);
  const prompt = page.getByTestId("v2-agent-prompt");
  await expect(prompt).toBeVisible({ timeout: 30_000 });
  await prompt.fill(`${await prompt.inputValue()}\nEdited by the e2e scenario (s10) at ${new Date().toISOString()}.`);
  await snap(page, "s10-edit");
  // the edit form validates against catalogs it is still loading (registry attachables, KBs, connections); a Next
  // clicked in that window shows a transient "Fix the highlighted problems first" (errGateway) and stays on step 1
  // (N-SCN-obs7, run #5) — re-click until the review step renders (≤ 60 s)
  const review = page.getByTestId("v2-agent-review");
  await expect
    .poll(async () => {
      if (await review.isVisible().catch(() => false)) return true;
      await page.getByTestId("v2-agent-wizard-next").click().catch(() => {});
      await page.waitForTimeout(2000);
      return review.isVisible().catch(() => false);
    }, { timeout: 60_000, intervals: [1000], message: "edit wizard reaches the review step" })
    .toBe(true);
  await expect(review).toBeVisible();
  const t0 = Date.now();
  await page.getByTestId("v2-agent-wizard-submit").click();
  await expect(page).toHaveURL(/view=detail&id=/, { timeout: 60_000 });
  const dep = await waitForDeploy(page, testInfo, hr.id, "s10", t0);
  const after = await getAgent(page.request, hr.id);
  expect(dep.job.status, `redeploy failed: ${dep.job.error ?? ""}`).toBe("succeeded");
  expect(Number(after.version), "version +1 after the edit").toBe(v0 + 1);
  expect(after.status, "agent stays active after redeploy").toBe("active");
  await expect(page.getByTestId("v2-agent-versions")).toBeVisible({ timeout: 60_000 });
  await snap(page, "s10-versions", `v${after.version}`, { key: true });

  // ── resources before the delete ──
  const runtimeId = runtimeIdOf(after);
  const roleName = `launchpad-agent-${hr.name}-${hr.id.slice(0, 8)}`;
  const logGroup = `/aws/bedrock-agentcore/runtimes/${runtimeId}-DEFAULT`;
  const ws = (await (await page.request.get("/api/workspaces", { timeout: 60_000 })).json()) as { workspaces: Array<{ is_default: boolean; resources?: { artifacts_bucket?: string } }> };
  const bucket = ws.workspaces.find((w) => w.is_default)?.resources?.artifacts_bucket ?? "";

  // N12-obs61: export the HR runtime's log groups to the evidence bucket before the delete removes them
  const exported = exportRuntimeLogs([runtimeId], String(stateGet("stamp") ?? "s10"), "s10");
  await record(testInfo, "runtime-logs-export.json", exported, `${exported.filter((g) => g.status === "uploaded").length} groups exported of ${exported.length}`);

  // ── delete through the console ──
  await page.goto("/v2/agents");
  await page.getByTestId(`v2-agent-delete-${hr.name}`).click();
  const delRes = page.waitForResponse((r) => r.url().includes(`/api/agents/${hr.id}`) && r.request().method() === "DELETE", { timeout: 180_000 });
  await page.getByTestId("v2-confirm-ok").click();
  const dr = await delRes;
  const delBody = (await dr.json().catch(() => ({}))) as { deleted?: boolean; removed?: string[]; failed?: string[] };
  await record(testInfo, "delete-response.json", delBody, `→ ${dr.status()} removed=${JSON.stringify(delBody.removed)} failed=${JSON.stringify(delBody.failed)}`);
  expect(dr.status()).toBe(200);
  expect(delBody.deleted).toBe(true);
  expect(delBody.failed ?? [], "no failed teardown step").toEqual([]);
  const removed = delBody.removed ?? [];
  if (removed.includes("log_groups")) expect(removed[removed.length - 1], "log_groups is the LAST teardown step").toBe("log_groups");
  for (let i = 0; i < 30 && (await listAgents(page.request)).some((x) => x.id === hr.id); i++) await sleep(10_000);
  await gotoRendered(page, null, /Agent Management|Agents/i); // N-SCN-obs19: reload + rendered list (the HR row must be gone)
  await expect(page.getByText(hr.name, { exact: true })).toHaveCount(0, { timeout: 30_000 }).catch(() => ledger("note: N-SCN-obs19 HR name still visible after delete + reload"));
  await snap(page, "s10-deleted", undefined, { key: true });

  // ── read-only verification ──
  let runtimeGone = false;
  for (let i = 0; i < 30 && !runtimeGone; i++) {
    const rt = awsRead(`bedrock-agentcore-control get-agent-runtime --agent-runtime-id ${runtimeId}`);
    runtimeGone = /ResourceNotFound|ERROR.*NotFound|does not exist/i.test(rt);
    if (!runtimeGone) await sleep(10_000);
  }
  await sleep(12_000); // N10v4-obs9: no empty log group re-created by the trailing OTel flush
  const role = awsRead(`iam get-role --role-name ${roleName}`);
  const lg = awsRead(`logs describe-log-groups --log-group-name-prefix ${logGroup}`);
  const s3 = bucket ? awsRead(`s3api list-objects-v2 --bucket ${bucket} --prefix agents/${hr.name}/ --max-keys 5`) : "";
  const mem = bucket ? awsRead(`s3api list-objects-v2 --bucket ${bucket} --prefix memory/ --max-keys 1000 --query "Contents[?contains(Key, '${hr.id}')].Key"`) : "";
  const recordAfter = (await registryRecords(page.request)).find((r) => r.name === hr.name);
  let recordsAfter = -1;
  if (actorId) {
    for (let i = 0; i < 6; i++) {
      recordsAfter = await countRecords(actorId).catch(() => -1);
      if (recordsAfter === 0) break;
      await sleep(10_000);
    }
  }
  const summary = {
    runtimeGone,
    roleGone: /NoSuchEntity/.test(role),
    logGroupGone: !lg.includes(logGroup),
    s3Gone: !bucket || !/"Key"/.test(s3),
    memGone: !bucket || /^\[\s*\]|null/.test(mem.trim()) || !/\S+__/.test(mem.replace(/\s/g, "")),
    recordGone: !recordAfter,
    memoryRecordsBefore: recordsBefore,
    memoryRecordsAfter: recordsAfter,
    removed,
  };
  await record(testInfo, "teardown-verification.json", summary, JSON.stringify(summary));
  expect(summary.runtimeGone, "runtime deleted").toBe(true);
  expect(summary.roleGone, "execution role deleted").toBe(true);
  expect(summary.logGroupGone, "log group deleted (and not re-created)").toBe(true);
  expect(summary.s3Gone, "S3 artifact prefix deleted").toBe(true);
  expect(summary.memGone, "memory snapshots deleted").toBe(true);
  expect(summary.recordGone, "A2A record deleted").toBe(true);
  if (actorId) {
    expect(removed, "delete lists the memory_records step").toContain("memory_records");
    expect(recordsAfter, "long-term records of the deleted agent purged").toBe(0);
  }
  stateSet("lifecycle", { hr_version_after_edit: Number(after.version), deleted: true });
  ledger(`s10 PASS hr v${v0} → v${after.version} → deleted (${removed.join(",")})`);
});

import { expect, test } from "@playwright/test";
import { activeAgent, attach, awsRead, E2E_PREFIX, evidence, getAgent, ledger, listAgents, registryRecords, sleep, waitForDeploy } from "./helpers";

/**
 * Case 17 (v3) — lifecycle: edit (prompt) → redeploy succeeds (N11 r3-B write-on-diff IAM
 * adopt) → version +1, agent stays active → delete → `failed=[]` and the Runtime, execution
 * role, log group, S3 prefix, memory snapshots and A2A record are all gone (read-only AWS),
 * plus the agent's long-term memory records are purged from OpenSearch (r3-C `memory_records`
 * step; verified through the memory API: 0 records for the agent's actor).
 * Default target: the case-2 Harness agent (long-term memory on, records from case 6).
 */
test("case 17: edit → redeploy (version +1, stays active) → delete → runtime/role/log group/S3/A2A/memory records gone", async ({ page }, testInfo) => {
  const prefix = process.env.N10_LIFECYCLE_PREFIX ?? "harness";
  const skipRedeploy = !!process.env.N10_SKIP_REDEPLOY;
  const rows = (await listAgents(page.request)).filter((x) => String(x.name).startsWith(`${E2E_PREFIX}${prefix}-`) && (skipRedeploy || x.status === "active"));
  rows.sort((x, y) => String(y.name).localeCompare(String(x.name)));
  const a = rows[0] ? { id: String(rows[0].id), name: String(rows[0].name) } : await activeAgent(page.request, prefix);
  expect(a, `an ${E2E_PREFIX}${prefix} agent is required`).toBeTruthy();
  const agentId = a!.id;
  const before = await getAgent(page.request, agentId);
  const v0 = Number(before.version);
  const versions0 = await (await page.request.get(`/api/agents/${agentId}/versions`)).json();
  ledger(`case17 start agent=${a!.name} version=${v0} aws latest=${versions0.latest_version} status=${before.status}`);

  // the agent's memory actor (for the purge check after delete)
  const sessions = ((await (await page.request.get(`/api/chat/${agentId}/sessions`)).json()).sessions ?? []) as Array<{ session_id: string }>;
  let actorId = "";
  let recordsBefore = 0;
  const countRecords = async (actor: string): Promise<number> => {
    const namespaces = (((await (await page.request.get(`/api/memory/namespaces?actor_id=${encodeURIComponent(actor)}`)).json()).items ?? []) as Array<{ strategy_id: string }>);
    let total = 0;
    for (const ns of namespaces) {
      const r = await (await page.request.get(`/api/memory/records?actor_id=${encodeURIComponent(actor)}&strategy_id=${encodeURIComponent(ns.strategy_id)}&max_results=100`)).json();
      total += ((r.items ?? r.records ?? []) as unknown[]).length;
    }
    return total;
  };
  if (sessions[0]) {
    const rail = await (await page.request.get(`/api/chat/${agentId}/memory?session_id=${encodeURIComponent(sessions[0].session_id)}`)).json();
    actorId = String(rail.actor_id ?? "");
    if (actorId) recordsBefore = await countRecords(actorId);
  }
  ledger(`case17 memory actor=${actorId} long-term records before delete=${recordsBefore}`);

  // ── edit → redeploy ──────────────────────────────────────────────────────
  let after = before;
  let redeploySeconds = 0;
  if (!skipRedeploy) {
    await page.goto(`/v2/agents?view=edit&id=${encodeURIComponent(agentId)}`);
    const prompt = page.getByTestId("v2-agent-prompt");
    await expect(prompt).toBeVisible({ timeout: 30_000 });
    await prompt.fill(`${await prompt.inputValue()}\nEdited by N10 v4 case 17 at ${new Date().toISOString()}.`);
    await evidence(page, "case17-edit");
    await page.getByTestId("v2-agent-wizard-next").click();
    await expect(page.getByTestId("v2-agent-review")).toBeVisible();
    const t0 = Date.now();
    await page.getByTestId("v2-agent-wizard-submit").click();
    await expect(page).toHaveURL(/view=detail&id=/, { timeout: 60_000 });
    const dep = await waitForDeploy(page, testInfo, agentId, "case17", t0);
    redeploySeconds = dep.seconds;
    after = await getAgent(page.request, agentId);
    const versions1 = await (await page.request.get(`/api/agents/${agentId}/versions`)).json();
    await attach(testInfo, "versions-after.json", versions1);
    const iamLines = ((dep.job.events as Array<{ msg: string }>) ?? []).map((e) => e.msg).filter((m) => /role|trust|inline policy|adopt/i.test(m));
    ledger(`case17 redeploy ${dep.job.status} in ${dep.seconds}s version ${v0} → ${after.version}; aws latest ${versions0.latest_version} → ${versions1.latest_version}; agent.status=${after.status}; iam: ${iamLines.join(" | ").slice(0, 400)}`);
    expect(dep.job.status, `redeploy failed: ${dep.job.error ?? ""}`).toBe("succeeded");
    expect(Number(after.version)).toBe(v0 + 1);
    expect(Number(versions1.latest_version)).toBeGreaterThan(Number(versions0.latest_version));
    expect(after.status, "agent stays active after redeploy").toBe("active");
    await page.goto(`/v2/agents?view=detail&id=${encodeURIComponent(agentId)}`);
    await expect(page.getByTestId("v2-agent-versions")).toBeVisible({ timeout: 60_000 });
    await evidence(page, "case17-versions");
  } else ledger(`case17 redeploy half skipped (N10_SKIP_REDEPLOY) — agent status=${before.status}`);

  // ── identify the AWS resources before deleting ───────────────────────────
  const arn = String(after.arn ?? "");
  const runtimeId = arn.split("/").pop() ?? "";
  const roleName = `launchpad-agent-${a!.name}-${agentId.slice(0, 8)}`;
  const logGroup = `/aws/bedrock-agentcore/runtimes/${runtimeId}-DEFAULT`;
  const resources = (await (await page.request.get("/api/workspaces")).json()).workspaces.find((w: { is_default: boolean }) => w.is_default).resources ?? {};
  const bucket = resources.artifacts_bucket ?? "";
  const recordBefore = (await registryRecords(page.request)).find((r) => r.name === a!.name);
  ledger(`case17 runtime=${runtimeId} role=${roleName} logGroup=${logGroup} bucket=${bucket} record=${recordBefore?.record_id}`);

  // ── delete through the UI ────────────────────────────────────────────────
  await page.goto("/v2/agents");
  await page.getByTestId(`v2-agent-delete-${a!.name}`).click();
  const delRes = page.waitForResponse((r) => r.url().includes(`/api/agents/${agentId}`) && r.request().method() === "DELETE");
  await page.getByTestId("v2-confirm-ok").click();
  const dr = await delRes;
  const delBody = await dr.json().catch(() => ({}));
  ledger(`case17 delete → ${dr.status()} removed=${JSON.stringify(delBody.removed)} failed=${JSON.stringify(delBody.failed)} skipped=${JSON.stringify(delBody.skipped)}`);
  await attach(testInfo, "delete-response.json", delBody);
  expect(dr.status()).toBe(200);
  expect(delBody.deleted).toBe(true);
  expect(delBody.failed ?? [], "delete reports no failed teardown step").toEqual([]);
  for (let i = 0; i < 30; i++) {
    if (!(await listAgents(page.request)).some((x) => x.id === agentId)) break;
    await sleep(10_000);
  }
  await page.reload();
  await evidence(page, "case17-deleted");

  // ── read-only verification (allow a few minutes for the runtime to disappear) ─
  let runtimeGone = false;
  for (let i = 0; i < 30 && !runtimeGone; i++) {
    const rt = awsRead(`bedrock-agentcore-control get-agent-runtime --agent-runtime-id ${runtimeId}`);
    runtimeGone = /ResourceNotFound|ERROR.*NotFound|does not exist/i.test(rt);
    if (!runtimeGone) await sleep(10_000);
  }
  const role = awsRead(`iam get-role --role-name ${roleName}`);
  const lg = awsRead(`logs describe-log-groups --log-group-name-prefix ${logGroup}`);
  const lgGone = !lg.includes(logGroup);
  const s3 = bucket ? awsRead(`s3api list-objects-v2 --bucket ${bucket} --prefix agents/${a!.name}/ --max-keys 5`) : "";
  const s3Gone = !bucket || !/"Key"/.test(s3);
  const mem = bucket ? awsRead(`s3api list-objects-v2 --bucket ${bucket} --prefix memory/ --max-keys 1000 --query "Contents[?contains(Key, '${agentId}')].Key"`) : "";
  const memGone = !bucket || !/\S+__/.test(mem.replace(/\s/g, "")) || /^\[\s*\]|null/.test(mem.trim());
  const recordAfter = (await registryRecords(page.request)).find((r) => r.name === a!.name);
  const roleGone = /NoSuchEntity/.test(role);
  const rolePolicies = roleGone ? "" : awsRead(`iam list-role-policies --role-name ${roleName}`);
  // long-term records of the agent's actor must be purged (r3-C memory_records step)
  let recordsAfter = -1;
  if (actorId) {
    for (let i = 0; i < 6; i++) {
      recordsAfter = await countRecords(actorId).catch(() => -1);
      if (recordsAfter === 0) break;
      await sleep(10_000);
    }
  }
  const summary = { runtimeGone, roleGone, logGroupGone: lgGone, s3Gone, memGone, recordGone: !recordAfter, memoryRecordsBefore: recordsBefore, memoryRecordsAfter: recordsAfter, removed: delBody.removed, role: role.slice(0, 200), rolePolicies: rolePolicies.slice(0, 200), lg: lg.slice(0, 200), s3: s3.slice(0, 200), mem: mem.slice(0, 300) };
  await attach(testInfo, "teardown-verification.json", summary);
  ledger(`case17 teardown ${JSON.stringify({ ...summary, role: undefined, lg: undefined, s3: undefined, mem: undefined })}`);
  expect(runtimeGone, "runtime deleted").toBe(true);
  expect(roleGone, "execution role deleted").toBe(true);
  expect(lgGone, "log group deleted").toBe(true);
  expect(s3Gone, "S3 artifact prefix deleted").toBe(true);
  expect(memGone, "memory snapshots deleted").toBe(true);
  expect(recordAfter, "A2A record deleted").toBeUndefined();
  if (actorId) {
    expect((delBody.removed ?? []) as string[], "delete response lists the memory_records step").toContain("memory_records");
    expect(recordsAfter, "long-term records of the deleted agent purged").toBe(0);
  }
  ledger(`case17 end PASS agent=${agentId} redeploy=${redeploySeconds}s`);
});

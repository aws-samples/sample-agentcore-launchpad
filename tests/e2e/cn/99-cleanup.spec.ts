import { expect, test } from "@playwright/test";
import { awsRead, E2E_PREFIX, evidence, getAgent, isE2E, ledger, listAgents, sleep, stateGet } from "./helpers";

/**
 * Tear down the e2e test data through the console, exactly as a user would:
 * every `e2e2-…` agent except the kept one (N10_KEEP, default `harness` — the
 * case-2/6 agent whose sessions back the observability evidence), every
 * `e2e2-…` knowledge base, the imported sample skill record, the case-12 member
 * account and the case-11 assistant conversations.
 */
test("cleanup: delete e2e agents (except the kept one), KBs and the skill record via the UI", async ({ page }) => {
  // N10_KEEP: a prefix ("harness" → keep every e2e4-harness-*) or an exact agent name. Round 4 keeps
  // nothing of its own (the keepers are round-1/3 agents outside the e2e4- prefix).
  const keep = process.env.N10_KEEP ?? "none";
  const isKept = (name: unknown) => String(name) === keep || String(name).startsWith(`${E2E_PREFIX}${keep}-`);
  ledger(`cleanup start keep=${E2E_PREFIX}${keep}-*`);

  // agents — never a system preset row (`eval-worker` / `launchpad-eval-worker*`, the architect preset; N12-bug6)
  const isSystem = (a: Record<string, unknown>) => !!a.system_key || /^launchpad-eval-worker|^eval-worker|solution-architect/.test(String(a.name));
  const agents = (await listAgents(page.request)).filter((a) => isE2E(a.name) && !isKept(a.name) && !isSystem(a));
  await page.goto("/v2/agents");
  await expect(page.getByTestId("v2-agents-table")).toBeVisible();
  // N10v4-obs9 re-check: the delete removes the log groups LAST (after the runtime is gone) and no
  // empty log group is re-created by the runtime's trailing OTel flush
  const logGroups: Array<{ name: string; group: string; removed: string[] }> = [];
  for (const a of agents) {
    const name = String(a.name);
    const runtimeId = String(((await getAgent(page.request, String(a.id)).catch(() => ({}))) as { arn?: string }).arn ?? "").split("/").pop() ?? "";
    const btn = page.getByTestId(`v2-agent-delete-${name}`);
    if (!(await btn.isVisible().catch(() => false))) {
      ledger(`cleanup agent ${name}: no delete button (status=${a.status})`);
      continue;
    }
    const delRes = page.waitForResponse((r) => r.url().includes(`/api/agents/${a.id}`) && r.request().method() === "DELETE", { timeout: 120_000 }).catch(() => null);
    await btn.click();
    await page.getByTestId("v2-confirm-ok").click();
    const dr = await delRes;
    const body = dr ? ((await dr.json().catch(() => ({}))) as { removed?: string[]; failed?: string[] }) : {};
    const removed = body.removed ?? [];
    ledger(`cleanup agent ${name} delete → ${dr?.status() ?? "-"} removed=${JSON.stringify(removed)} failed=${JSON.stringify(body.failed ?? [])}`);
    if (runtimeId) logGroups.push({ name, group: `/aws/bedrock-agentcore/runtimes/${runtimeId}-DEFAULT`, removed });
    await page.waitForTimeout(1500);
  }
  for (let i = 0; i < 40; i++) {
    const left = (await listAgents(page.request)).filter((a) => agents.some((x) => x.id === a.id));
    if (left.length === 0) break;
    await sleep(10_000);
  }
  await page.reload();
  await evidence(page, "cleanup-agents-after");
  if (logGroups.length) {
    await sleep(12_000);
    for (const lg of logGroups) {
      const out = awsRead(`logs describe-log-groups --log-group-name-prefix ${lg.group} --query "logGroups[].{n:logGroupName,b:storedBytes,c:creationTime}"`);
      const recreated = out.includes(lg.group);
      const lastStep = lg.removed[lg.removed.length - 1];
      ledger(`cleanup obs9 ${lg.name}: removed order ends with=${lastStep} log group re-created=${recreated} ${recreated ? out.replace(/\s/g, "").slice(0, 200) : ""}`);
      if (lg.removed.includes("log_groups")) expect(lastStep, `${lg.name}: log_groups is the LAST teardown step`).toBe("log_groups");
      expect(recreated, `${lg.name}: no empty log group re-created after the delete (N10v4-obs9)`).toBe(false);
    }
  }

  // knowledge bases
  const kbs = ((await (await page.request.get("/api/knowledge-bases")).json()).items as Array<{ kb_id: string; name: string }>).filter((k) => isE2E(k.name));
  if (kbs.length) {
    await page.goto("/v2/knowledge-bases");
    for (const kb of kbs) {
      const btn = page.getByTestId(`v2-kb-delete-${kb.name}`);
      if (await btn.isVisible().catch(() => false)) {
        await btn.click();
        await page.getByTestId("v2-confirm-ok").click();
        ledger(`cleanup kb ${kb.name} (${kb.kb_id}) delete requested`);
        await page.waitForTimeout(2000);
      } else {
        ledger(`cleanup kb ${kb.name}: no delete button`);
      }
    }
    await page.reload();
    await evidence(page, "cleanup-kbs-after");
  }

  // Skill Lab (v4): the jobs this run created (on the shared sample skill → bounded by the run start) + e2e4 task sets
  const runStart = stateGet<string>("run_start") ?? new Date(0).toISOString();
  const slJobs = ((await (await page.request.get("/api/skill-lab/jobs")).json().catch(() => [])) ?? []) as Array<{ id: string; type: string; status: string; created_at?: string; skill_source?: { name?: string } }>;
  for (const j of slJobs.filter((j) => isE2E(j.skill_source?.name) || (j.created_at && String(j.created_at) >= runStart))) {
    const r = await page.request.delete(`/api/skill-lab/jobs/${j.id}`);
    ledger(`cleanup skill-lab job ${j.id} (${j.type} ${j.status}) → ${r.status()}`);
  }
  const slTasksets = ((await (await page.request.get("/api/skill-lab/tasksets")).json().catch(() => [])) ?? []) as Array<{ id: string; name: string }>;
  for (const t of slTasksets.filter((t) => isE2E(t.name))) {
    const r = await page.request.delete(`/api/skill-lab/tasksets/${t.id}`);
    ledger(`cleanup skill-lab taskset ${t.name} (${t.id}) → ${r.status()}`);
  }

  // registry: the imported sample skill, the e2e3 Skill Lab skill, any A2A records left by deleted agents,
  // and (N10v3-bug6) the records the uninstalled architect preset left behind
  const records = (await (await page.request.get("/api/registry/records")).json()).records as Array<{ record_id: string; name: string }>;
  const presetGone = (((await (await page.request.get("/api/system-agents")).json()).presets ?? []) as Array<{ key: string; status: string }>).find((p) => p.key === "aws-agent-solution-architect")?.status === "not_installed";
  // round 4: `expense-report-writer` and the `sample-*` skills are platform-seeded — kept (N11-obs31 wants exactly one APPROVED sample record)
  const victims = records.filter((r) => isE2E(r.name) || agents.some((a) => a.name === r.name) || (presetGone && r.name === "aws-agent-solution-architect"));
  for (const r of victims) {
    await page.goto(`/v2/registry?view=detail&id=${encodeURIComponent(r.record_id)}`);
    const del = page.getByTestId("v2-registry-delete");
    if (await del.isVisible({ timeout: 10_000 }).catch(() => false)) {
      await del.click();
      await page.getByTestId("v2-confirm-ok").click();
      ledger(`cleanup registry record ${r.name} (${r.record_id}) delete requested`);
      await page.waitForTimeout(1500);
    }
  }

  // users registered by case 12 / spec 31 (admin UI → delete; the product API when the row has no delete action — N10v3-obs5)
  const users = ((await (await page.request.get("/api/users")).json()).items as Array<{ id: string; username: string }>).filter((u) => u.username.startsWith(`${E2E_PREFIX}member-`));
  if (users.length) {
    await page.goto("/v2/users");
    for (const u of users) {
      const btn = page.getByTestId(`v2-users-delete-${u.username}`);
      if (await btn.isVisible({ timeout: 5000 }).catch(() => false)) {
        await btn.click();
        const ok = page.getByTestId("v2-confirm-ok");
        if (await ok.isVisible({ timeout: 2000 }).catch(() => false)) await ok.click();
        ledger(`cleanup user ${u.username} delete requested`);
        await page.waitForTimeout(1000);
      } else {
        const r = await page.request.delete(`/api/users/${u.id}`);
        ledger(`cleanup user ${u.username} (no delete action in the Users page) → DELETE /api/users → ${r.status()}`);
      }
    }
  }
  // the architect system preset installed by case 11 → uninstall through the /agents/new presets panel
  // (kept after r13-a retired the classic console; it has no V2 twin yet).
  // N-ARCH-obs7: only on explicit request (N10_UNINSTALL_PRESET=1) — on an environment where the
  // preset is meant to stay (every round-5+ default host) the block is skipped and ledgered.
  const presets = ((await (await page.request.get("/api/system-agents")).json()).presets ?? []) as Array<{ key: string; status: string }>;
  const architect = presets.find((p) => p.key === "aws-agent-solution-architect");
  if (architect && architect.status !== "not_installed" && process.env.N10_UNINSTALL_PRESET !== "1") {
    ledger(`cleanup system preset aws-agent-solution-architect: kept (status=${architect.status}; set N10_UNINSTALL_PRESET=1 to uninstall — N-ARCH-obs7)`);
  } else if (architect && architect.status !== "not_installed") {
    await page.goto("/agents/new");
    const un = page.getByTestId("uninstall-aws-agent-solution-architect");
    if (await un.isVisible({ timeout: 30_000 }).catch(() => false)) {
      await un.click();
      const dialog = page.getByRole("alertdialog");
      if (await dialog.isVisible({ timeout: 3000 }).catch(() => false)) await dialog.getByRole("button").last().click();
      ledger("cleanup system preset aws-agent-solution-architect uninstall requested");
      for (let i = 0; i < 30; i++) {
        const now = ((await (await page.request.get("/api/system-agents")).json()).presets ?? []) as Array<{ key: string; status: string }>;
        if (now.find((p) => p.key === "aws-agent-solution-architect")?.status === "not_installed") break;
        await sleep(10_000);
      }
    } else ledger("cleanup system preset: no uninstall button visible");
  }
  // assistant conversations created by case 11 (the agent itself is deleted above)
  const convs = ((await (await page.request.get("/api/assistant/architect/conversations")).json().catch(() => ({ conversations: [] }))).conversations ?? []) as Array<{ id: string }>;
  for (const c of convs) {
    const r = await page.request.delete(`/api/assistant/architect/conversations/${c.id}`);
    ledger(`cleanup assistant conversation ${c.id} → ${r.status()}`);
  }

  const remainingAgents = (await listAgents(page.request)).map((a) => `${a.name}:${a.status}`);
  const remainingKbs = ((await (await page.request.get("/api/knowledge-bases")).json()).items as Array<{ name: string }>).map((k) => k.name);
  const remainingRecords = ((await (await page.request.get("/api/registry/records")).json()).records as Array<{ name: string; status: string }>).map((r) => `${r.name}:${r.status}`);
  ledger(`cleanup end agents=${JSON.stringify(remainingAgents)} kbs=${JSON.stringify(remainingKbs)} records=${JSON.stringify(remainingRecords)}`);
});

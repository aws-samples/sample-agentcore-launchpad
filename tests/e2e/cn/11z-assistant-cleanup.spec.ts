import { expect, test } from "@playwright/test";
import { awsRead, E2E_PREFIX, evidence, getAgent, isE2E, ledger, listAgents, sleep, stateGet } from "./helpers";

const PRESET = "aws-agent-solution-architect";

/**
 * arch-e2e teardown through the product: every `e2ea-…` agent (full teardown — the removed
 * list ends with `log_groups`), the `e2ea-member-*` account, and ONLY the assistant
 * conversations this run opened (ids in the shared state) — never the preset, never a
 * conversation of another user. Read-only AWS check afterwards: no `e2ea` runtime / role left.
 */
test("arch-6: delete the e2ea agents, member and conversations; preset stays installed", async ({ page }) => {
  ledger("arch-6 cleanup start");
  const isSystem = (a: Record<string, unknown>) => !!a.system_key || /^launchpad-eval-worker|^eval-worker|solution-architect/.test(String(a.name));
  const agents = (await listAgents(page.request)).filter((a) => isE2E(a.name) && !isSystem(a));
  await page.goto("/v2/agents");
  await expect(page.getByTestId("v2-agents-table")).toBeVisible();
  const results: Array<{ name: string; status: number | string; removed: string[]; failed: string[]; runtimeId: string }> = [];
  for (const a of agents) {
    const name = String(a.name);
    const arn = String(((await getAgent(page.request, String(a.id)).catch(() => ({}))) as { arn?: string }).arn ?? "");
    const runtimeId = arn.split("/").pop() ?? "";
    const btn = page.getByTestId(`v2-agent-delete-${name}`);
    if (!(await btn.isVisible().catch(() => false))) {
      ledger(`arch-6 agent ${name}: no delete button (status=${a.status})`);
      continue;
    }
    const delRes = page.waitForResponse((r) => r.url().includes(`/api/agents/${a.id}`) && r.request().method() === "DELETE", { timeout: 180_000 }).catch(() => null);
    await btn.click();
    await page.getByTestId("v2-confirm-ok").click();
    const dr = await delRes;
    const body = dr ? ((await dr.json().catch(() => ({}))) as { removed?: string[]; failed?: string[] }) : {};
    results.push({ name, status: dr?.status() ?? "-", removed: body.removed ?? [], failed: body.failed ?? [], runtimeId });
    ledger(`arch-6 agent ${name} delete → ${dr?.status() ?? "-"} removed=${JSON.stringify(body.removed ?? [])} failed=${JSON.stringify(body.failed ?? [])}`);
    await page.waitForTimeout(1500);
  }
  for (let i = 0; i < 40; i++) {
    const left = (await listAgents(page.request)).filter((a) => agents.some((x) => x.id === a.id));
    if (left.length === 0) break;
    await sleep(10_000);
  }
  await page.reload();
  await evidence(page, "arch6-agents-after");
  for (const r of results) {
    const last = r.removed[r.removed.length - 1];
    expect(r.failed, `${r.name}: nothing failed`).toEqual([]);
    if (r.removed.includes("log_groups")) expect(last, `${r.name}: log_groups is the LAST teardown step`).toBe("log_groups");
  }

  // the member account (admin Users page, API fallback as in 99-cleanup)
  const users = ((await (await page.request.get("/api/users")).json()).items as Array<{ id: string; username: string }>).filter((u) => u.username.startsWith(`${E2E_PREFIX}member-`));
  if (users.length) {
    await page.goto("/v2/users");
    for (const u of users) {
      const btn = page.getByTestId(`v2-users-delete-${u.username}`);
      if (await btn.isVisible({ timeout: 5000 }).catch(() => false)) {
        await btn.click();
        const ok = page.getByTestId("v2-confirm-ok");
        if (await ok.isVisible({ timeout: 2000 }).catch(() => false)) await ok.click();
        ledger(`arch-6 user ${u.username} delete requested (UI)`);
        await page.waitForTimeout(1000);
      } else {
        const r = await page.request.delete(`/api/users/${u.id}`);
        ledger(`arch-6 user ${u.username} → DELETE /api/users → ${r.status()}`);
      }
    }
    await evidence(page, "arch6-users-after");
  }

  // only the conversations this run opened (never another user's)
  for (const id of stateGet<string[]>("arch_conversations") ?? []) {
    const r = await page.request.delete(`/api/assistant/architect/conversations/${id}`);
    ledger(`arch-6 conversation ${id} → ${r.status()} ${(await r.text()).slice(0, 160).replace(/\n/g, " ")}`);
  }

  // read-only AWS check + the preset is untouched
  await sleep(10_000);
  const runtimes = awsRead(`bedrock-agentcore-control list-agent-runtimes --query "agentRuntimes[?contains(agentRuntimeName, 'e2ea')].{n:agentRuntimeName,s:status}"`);
  const roles = awsRead(`iam list-roles --query "Roles[?contains(RoleName, 'e2ea')].RoleName"`);
  const presets = ((await (await page.request.get("/api/system-agents")).json()).presets ?? []) as Array<{ key: string; status: string; installed_release?: string }>;
  const preset = presets.find((p) => p.key === PRESET);
  const remaining = (await listAgents(page.request)).map((a) => `${a.name}:${a.status}`);
  const remainingUsers = ((await (await page.request.get("/api/users")).json()).items as Array<{ username: string }>).map((u) => u.username);
  ledger(`arch-6 aws e2ea runtimes=${runtimes.replace(/\s/g, "").slice(0, 200)} e2ea roles=${roles.replace(/\s/g, "").slice(0, 200)} preset=${preset?.status}/${preset?.installed_release} agents=${JSON.stringify(remaining)} users=${JSON.stringify(remainingUsers)}`);
  expect(preset?.status, "preset still installed").toBe("active");
  expect(runtimes.includes("e2ea"), "no e2ea runtime left").toBe(false);
  expect(roles.includes("e2ea"), "no e2ea role left").toBe(false);
  expect(remaining.some((n) => n.startsWith(E2E_PREFIX)), "no e2ea agent left").toBe(false);
  ledger("arch-6 cleanup end PASS");
});

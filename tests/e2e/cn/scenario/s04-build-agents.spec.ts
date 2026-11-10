import { expect, test } from "@playwright/test";
import { approveRecordViaUi, chatOnce, createStrandsViaWizard, evidence, getAgent, ledger, looksLikeError, registryRecords, waitForDeploy } from "../helpers";
import { softly, FIFTEEN_MIN, GATEWAY_TARGET, PROMPTS, record, runtimeIdOf, scnName, snap, stateRequire, stateSet, stepGate, TOKENS } from "./scn";

/**
 * s04 — build the agents (plan §3 step 4; REQUIREMENTS §4.1 cases 2, 3, 15, 16 thresholds).
 *
 * Three deploys, in dependency order, each `active` ≤ 15 min:
 *   a) `e2es-payroll` — Strands zip_runtime, protocol A2A: the payroll specialist the HR
 *      assistant delegates to (only `protocol=a2a` agents are A2A-attachable).
 *   b) `e2es-hr` — 方式B Harness HR assistant: KB (s01) + hr-database gateway (s02) + the
 *      published skill (s03) + long-term memory + Code Interpreter + the payroll A2A peer.
 *      Main subject of s05–s07, s08a, s09, s10.
 *   c) `e2es-hr-runtime` — Strands zip_runtime HTTP twin (gateway + KB), subject of s08b/s08c.
 *      Historically the only method the optimization routers accepted
 *      (`not-http-runtime` for a Harness); since cn/r10-d
 *      (N12-obs33/34) a Runtime-backed Harness is eligible too (`MethodCapabilities`,
 *      `docs/cn/interfaces/evaluation-engine.md` §9a); the twin is kept unchanged so the
 *      storyline's s08 subject and its assertions stay as they were (comment-only update).
 */
test.describe.configure({ mode: "serial" });

test("s04a — payroll specialist (Strands, protocol A2A) deploys ≤ 15 min, record APPROVED, live card 200, attachable, direct Chat carries its token", async ({ page }, testInfo) => {
  stepGate("s04");
  test.setTimeout(40 * 60 * 1000);
  const name = scnName("payroll");
  const t0 = Date.now();
  const id = await createStrandsViaWizard(page, {
    name,
    prompt: PROMPTS.payroll,
    protocol: "a2a",
    a2aSkill: { name: "payroll-netpay", description: "Computes Octank net pay, pension and tax", tags: "payroll,hr,octank" },
    evidencePrefix: "s04a",
  });
  const dep = await waitForDeploy(page, testInfo, id, "s04a", t0);
  expect(dep.job.status, `deploy failed: ${dep.job.error ?? ""}`).toBe("succeeded");
  expect(dep.agent.status).toBe("active");
  expect(dep.seconds, "A2A server active ≤ 15 min").toBeLessThanOrEqual(FIFTEEN_MIN);

  let rec = (await registryRecords(page.request)).find((r) => r.name === name);
  expect(rec, "A2A record auto-created by the register stage").toBeTruthy();
  if (rec!.status !== "APPROVED") ledger(`s04a record ${rec!.record_id} → ${await approveRecordViaUi(page, rec!.record_id)}`);
  rec = (await registryRecords(page.request)).find((r) => r.name === name);
  expect(rec!.status).toBe("APPROVED");
  const card = await page.request.get(`/api/registry/records/${rec!.record_id}/live-agent-card`, { timeout: 60_000 });
  expect(card.status(), "live agent card").toBe(200);
  const attachables = (await (await page.request.get("/api/registry/attachables?refresh=true", { timeout: 60_000 })).json()) as { agents?: Array<{ name: string; attachable: boolean; attachability_reason?: string | null }> };
  const entry = (attachables.agents ?? []).find((a) => a.name === name);
  await record(testInfo, "attachables-agents.json", attachables.agents, `payroll attachable=${entry?.attachable} reason=${entry?.attachability_reason ?? ""}`);
  expect(entry?.attachable, `payroll attachable (${entry?.attachability_reason ?? ""})`).toBe(true);

  const chat = await chatOnce(page, id, "What is Octank's net pay for a gross salary of 10000?", { timeoutMs: 4 * 60 * 1000 });
  await record(testInfo, "payroll-direct-chat.txt", chat.answer, `${chat.seconds}s errors=${chat.errors}`);
  await snap(page, "s04a-payroll-direct-chat", undefined, { key: true });
  expect(chat.errors).toBe(0);
  expect(looksLikeError(chat.answer)).toBe(false);
  // model-behaviour (audit before run #10): the token instruction is soft, the contract is model text without an error
  if (!chat.answer.includes(TOKENS.payroll)) ledger(`soft: payroll direct chat answered without its token: ${chat.answer.slice(0, 120)}`);
  softly(testInfo, "payroll answer carries its token (soft)", () => expect(chat.answer, "payroll answer carries its token (soft)").toContain(TOKENS.payroll));
  stateSet("payroll", { id, name, record_id: rec!.record_id, deploy_seconds: dep.seconds });
  ledger(`s04a PASS payroll=${id} deploy=${dep.seconds}s`);
});

test("s04b — HR assistant (方式B Harness: KB + gateway + published skill + long-term memory + Code Interpreter + payroll A2A peer) deploys ≤ 15 min", async ({ page }, testInfo) => {
  stepGate("s04");
  test.setTimeout(40 * 60 * 1000);
  const kb = stateRequire("kb", "s01");
  const skill = stateRequire("skill", "s03");
  const payroll = stateRequire("payroll", "s04");
  const name = scnName("hr");

  await page.goto("/v2/agents");
  await page.getByTestId("v2-agent-new").click();
  await page.getByTestId("v2-agent-method-harness").click();
  await page.getByTestId("v2-agent-wizard-next").click();
  await page.getByTestId("v2-agent-name").fill(name);
  await page.getByTestId("v2-agent-prompt").fill(PROMPTS.hrAssistant);
  const check = async (listTestId: string, label: RegExp | string, what: string) => {
    const box = page.getByTestId(listTestId).locator("label", { hasText: label }).locator('input[type="checkbox"]');
    await expect(box, `${what} offered in the wizard`).toBeVisible({ timeout: 30_000 });
    await expect(box, `${what} attachable`).toBeEnabled();
    await box.check();
  };
  await check("v2-agent-builtins", /code interpreter/i, "Code Interpreter builtin");
  await check("v2-agent-gateways", GATEWAY_TARGET, `gateway target ${GATEWAY_TARGET}`);
  await check("v2-agent-a2a-peers", payroll.name, `A2A peer ${payroll.name}`);
  await check("v2-agent-skills", skill.name, `skill ${skill.name}`);
  await check("v2-agent-kbs", kb.name, `knowledge base ${kb.name}`);
  await page.getByTestId("v2-agent-long-term").check();
  await evidence(page, "s04b-wizard-configure");
  await page.getByTestId("v2-agent-wizard-next").click();
  await expect(page.getByTestId("v2-agent-review")).toBeVisible();
  await evidence(page, "s04b-wizard-review");
  const t0 = Date.now();
  await page.getByTestId("v2-agent-wizard-submit").click();
  await expect(page).toHaveURL(/view=detail&id=/, { timeout: 60_000 });
  const id = new URL(page.url()).searchParams.get("id")!;

  const row = await getAgent(page.request, id);
  const spec = row.spec as { model_id?: string; memory?: { long_term?: boolean }; tools?: Array<{ type: string; name?: string; config?: Record<string, unknown> }>; skills?: unknown[]; knowledge_bases?: unknown[] };
  await record(testInfo, "hr-spec.json", spec);
  const tools = spec.tools ?? [];
  expect(String(spec.model_id), "default model GLM 5.3").toMatch(/glm/i);
  expect(spec.memory?.long_term, "long-term memory on").toBe(true);
  expect(tools.some((t) => t.type === "builtin" && /code-interpreter/.test(String(t.name))), "spec carries the Code Interpreter builtin").toBe(true);
  expect(tools.some((t) => t.type === "a2a"), "spec carries one A2A ToolRef").toBe(true);
  expect(tools.some((t) => /gateway/.test(t.type)), "spec carries the gateway ToolRef").toBe(true);

  const dep = await waitForDeploy(page, testInfo, id, "s04b", t0);
  expect(dep.job.status, `deploy failed: ${dep.job.error ?? ""}`).toBe("succeeded");
  expect(dep.agent.status).toBe("active");
  expect(dep.seconds, "Harness active ≤ 15 min").toBeLessThanOrEqual(FIFTEEN_MIN);
  const rec = (await registryRecords(page.request)).find((r) => r.name === name);
  expect(rec, "registry record auto-created for the HR assistant").toBeTruthy();
  stateSet("hr", { id, name, version: Number(dep.agent.version), deploy_seconds: dep.seconds });
  ledger(`s04b PASS hr=${id} v${dep.agent.version} deploy=${dep.seconds}s record=${rec!.status}`);
});

test("s04c — HR runtime twin (Strands zip_runtime HTTP: gateway + KB) deploys ≤ 15 min and is canary-eligible", async ({ page }, testInfo) => {
  stepGate("s04");
  test.setTimeout(40 * 60 * 1000);
  const kb = stateRequire("kb", "s01");
  const name = scnName("hr-runtime");

  await page.goto("/v2/agents");
  await page.getByTestId("v2-agent-new").click();
  await page.getByTestId("v2-agent-method-zip_runtime").click();
  await page.getByTestId("v2-agent-wizard-next").click();
  await page.getByTestId("v2-agent-name").fill(name);
  await page.getByTestId("v2-agent-prompt").fill(PROMPTS.hrRuntime);
  const gw = page.getByTestId("v2-agent-gateways").locator("label", { hasText: GATEWAY_TARGET }).locator('input[type="checkbox"]');
  await expect(gw, "gateway target offered to the zip runtime").toBeEnabled();
  await gw.check();
  const kbBox = page.getByTestId("v2-agent-kbs").locator("label", { hasText: kb.name }).locator('input[type="checkbox"]');
  await expect(kbBox, "knowledge base offered to the zip runtime").toBeVisible();
  await kbBox.check();
  await evidence(page, "s04c-wizard-configure");
  await page.getByTestId("v2-agent-wizard-next").click();
  await expect(page.getByTestId("v2-agent-review")).toBeVisible();
  await evidence(page, "s04c-wizard-review");
  const t0 = Date.now();
  await page.getByTestId("v2-agent-wizard-submit").click();
  await expect(page).toHaveURL(/view=detail&id=/, { timeout: 60_000 });
  const id = new URL(page.url()).searchParams.get("id")!;

  const dep = await waitForDeploy(page, testInfo, id, "s04c", t0);
  expect(dep.job.status, `deploy failed: ${dep.job.error ?? ""}`).toBe("succeeded");
  expect(dep.agent.status).toBe("active");
  expect(dep.seconds, "zip runtime active ≤ 15 min").toBeLessThanOrEqual(FIFTEEN_MIN);
  expect(dep.agent.method).toBe("zip_runtime");
  const full = await getAgent(page.request, id);
  // N13-obs2: the configuration experiment (and its readiness probe) retired — the twin's optimization
  // eligibility is the canary verdict the agent row carries
  const capability = (full as { canary_capability?: { eligible?: boolean; reason_code?: string | null } }).canary_capability;
  await record(testInfo, "canary-capability.json", capability ?? null, `canary_capability ${JSON.stringify(capability ?? null)}`);
  expect(capability?.eligible, `zip_runtime HTTP agent is canary-eligible (${capability?.reason_code ?? "no reason"})`).toBe(true);
  stateSet("runtime", { id, name, resource_id: runtimeIdOf(full), version: Number(full.version), deploy_seconds: dep.seconds });
  await snap(page, "s04c-runtime-detail", `v${full.version}`, { key: true });
  ledger(`s04c PASS runtime=${id} v${full.version} deploy=${dep.seconds}s`);
});

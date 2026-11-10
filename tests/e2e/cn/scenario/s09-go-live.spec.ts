import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { chatOnce, EVIDENCE_DIR, getAgent, ledger, looksLikeError, registryRecords } from "../helpers";
import { softly, mask, record, runtimeEndpoints, snap, stateRequire, stateSet, stepGate, TOKENS } from "./scn";

/**
 * s09 — go live (plan §3 step 9; REQUIREMENTS §4.1 cases 14, 17 (version half), 22).
 * The promoted versions serve both entrances: the HR assistant (v+1 after ACCEPT) answers the
 * same question through Chat and the public /v1 API; the runtime twin (v+1 after PROMOTE,
 * canary endpoints gone — DEFAULT only) answers through Chat; the registry records of all three
 * agents are present; ops idempotency: /api/health, 13 capabilities, workspace ready and — when
 * the lead supplies it (SCENARIO_BOOTSTRAP_SUMMARY=<file>) — a bootstrap re-run that reads
 * "reused / no changes".
 */
const HOTLINE_PROMPT = "What is the HR hotline code? Reply with the code and nothing else.";

test("s09 — promoted versions live on Chat + /v1, registry records current, DEFAULT endpoint only, ops idempotency", async ({ page, request }, testInfo) => {
  stepGate("s09");
  test.setTimeout(30 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  const runtime = stateRequire("runtime", "s04");
  const payroll = stateRequire("payroll", "s04");
  const improve = stateRequire("improve", "s08");

  // ── HR assistant: version after ACCEPT, Chat + /v1 on the same version ──
  const hrRow = await getAgent(page.request, hr.id);
  const hrVersions = (await (await page.request.get(`/api/agents/${hr.id}/versions`, { timeout: 60_000 })).json()) as { latest_version?: number | string };
  await record(testInfo, "hr-agent.json", { version: hrRow.version, status: hrRow.status, latest: hrVersions.latest_version });
  expect(hrRow.status).toBe("active");
  expect(Number(hrRow.version), "HR assistant serves the version minted by ACCEPT").toBe(improve.hr_version_after_accept);
  expect(Number(hrVersions.latest_version), "AWS latest version matches").toBeGreaterThanOrEqual(Number(hrRow.version));
  await page.goto(`/v2/chat?agent=${encodeURIComponent(hr.id)}`);
  const chat = await chatOnce(page, hr.id, HOTLINE_PROMPT);
  await record(testInfo, "hr-chat.txt", chat.answer, `${chat.seconds}s errors=${chat.errors}`);
  expect(chat.errors).toBe(0);
  expect(looksLikeError(chat.answer)).toBe(false);
  // the optimizer rewrote the prompt in s08a; whether the hotline instruction survived is recorded, not required
  softly(testInfo, "hotline instruction survived the prompt optimization", () => expect(chat.answer, "hotline instruction survived the prompt optimization").toMatch(new RegExp(TOKENS.hotline, "i")));
  await page.getByRole("tab", { name: /API/i }).click();
  await page.getByTestId("new-key").click();
  await expect(page.getByTestId("new-key-value")).toBeVisible({ timeout: 30_000 });
  const key = (await page.getByTestId("new-key-value").innerText()).trim();
  await page.screenshot({ path: `${EVIDENCE_DIR}/s09-api-key.png`, fullPage: true, mask: [page.getByTestId("new-key-value")] });
  const v1 = await request.post(`/v1/agents/${hr.id}/invoke`, { headers: { "X-Api-Key": key }, data: { prompt: HOTLINE_PROMPT }, timeout: 5 * 60 * 1000 });
  const v1Body = (await v1.json().catch(async () => ({ raw: await v1.text() }))) as { text?: string };
  await record(testInfo, "hr-v1.json", v1Body, `/v1 → ${v1.status()} ${mask(JSON.stringify(v1Body)).slice(0, 160)}`);
  expect(v1.status(), "/v1 on the promoted version").toBe(200);
  expect(looksLikeError(String(v1Body.text))).toBe(false);
  const bothCarry = new RegExp(TOKENS.hotline, "i").test(chat.answer) === new RegExp(TOKENS.hotline, "i").test(String(v1Body.text));
  if (!bothCarry) ledger(`soft: Chat and /v1 disagree on the hotline code (chat=${chat.answer.slice(0, 80)} | v1=${String(v1Body.text).slice(0, 80)})`);
  softly(testInfo, "Chat and /v1 agree on the hotline code (soft — model instruction, ruling Q3)", () => expect(bothCarry, "Chat and /v1 agree on the hotline code (soft — model instruction, ruling Q3)").toBe(true));
  const row = page.getByTestId("api-key-row").filter({ hasText: key.slice(0, 12) }).first();
  await row.getByRole("button", { name: /disable/i }).click();
  await page.getByTestId("v2-confirm-ok").click();
  await expect(row.getByText(/disabled/i)).toBeVisible({ timeout: 30_000 });
  expect((await request.post(`/v1/agents/${hr.id}/invoke`, { headers: { "X-Api-Key": key }, data: { prompt: HOTLINE_PROMPT } })).status()).toBe(401);

  // ── runtime twin: promoted version, DEFAULT endpoint only, Chat works ──
  const rtRow = await getAgent(page.request, runtime.id);
  expect(rtRow.status).toBe("active");
  expect(Number(rtRow.version), "runtime twin at or beyond the PROMOTE version").toBeGreaterThanOrEqual(improve.runtime_version_after_promote ?? 0);
  const eps = runtimeEndpoints(runtime.resource_id);
  await record(testInfo, "runtime-endpoints.json", eps, eps.map((e) => `${e.name}=${e.status}/v${e.liveVersion}`).join(","));
  expect(eps.some((e) => (e.name.startsWith("stable") || e.name.startsWith("treat")) && e.status !== "DELETING"), "no canary endpoint left (a DELETING one is on its way out)").toBeFalsy();
  await page.goto(`/v2/chat?agent=${encodeURIComponent(runtime.id)}`);
  const rtChat = await chatOnce(page, runtime.id, "What is the magic word? Reply with it only.");
  await record(testInfo, "runtime-chat.txt", rtChat.answer, `${rtChat.seconds}s errors=${rtChat.errors}`);
  expect(rtChat.errors).toBe(0);
  expect(looksLikeError(rtChat.answer)).toBe(false);
  softly(testInfo, "magic-word instruction survived the promoted prompt", () => expect(rtChat.answer, "magic-word instruction survived the promoted prompt").toMatch(new RegExp(TOKENS.runtimeMagic, "i")));
  await page.goto(`/v2/agents?view=detail&id=${runtime.id}`);
  await expect(page.getByTestId("v2-agent-versions")).toBeVisible({ timeout: 60_000 });
  await snap(page, "s09-runtime-versions", `v${rtRow.version}`, { key: true });

  // ── registry: the three agents' records are current ──
  const records = await registryRecords(page.request);
  for (const a of [hr, runtime, payroll]) {
    const rec = records.find((r) => r.name === a.name);
    expect(rec, `registry record of ${a.name}`).toBeTruthy();
    expect(["PENDING_APPROVAL", "APPROVED", "DRAFT"]).toContain(String(rec!.status));
  }
  expect((await page.request.get(`/api/registry/records/${payroll.record_id}/live-agent-card`, { timeout: 60_000 })).status(), "payroll live card still served").toBe(200);
  await page.goto("/v2/registry");
  await snap(page, "s09-registry");

  // ── ops idempotency (case 22) ──
  const health = await request.get("/api/health", { timeout: 60_000 });
  const hb = (await health.json()) as { status?: string; region?: string };
  expect(health.status()).toBe(200);
  expect(hb.status).toBe("ok");
  const caps = (await (await page.request.get("/api/capabilities", { timeout: 60_000 })).json()) as { features: string[] };
  expect(caps.features.length).toBeGreaterThanOrEqual(13);
  expect(caps.features).toContain("policy");
  const ws = (await (await page.request.get("/api/workspaces", { timeout: 60_000 })).json()) as { workspaces: Array<{ is_default: boolean; bootstrap_status: string }> };
  expect(ws.workspaces.find((w) => w.is_default)!.bootstrap_status).toBe("ready");
  await page.goto("/v2");
  await expect(page.getByTestId("v2-kpi-agents")).toBeVisible();
  await snap(page, "s09-overview", undefined, { key: true });
  const file = process.env.SCENARIO_BOOTSTRAP_SUMMARY ?? process.env.N10_BOOTSTRAP_SUMMARY;
  if (file && fs.existsSync(file)) {
    const summary = fs.readFileSync(path.resolve(file), "utf8");
    await record(testInfo, "bootstrap-summary.txt", summary);
    const bad = summary.split("\n").filter((l) => /\b[1-9]\d* created|CREATE_|UPDATE_|replaced|deleted/i.test(l) && !/reused|no changes|unchanged|skipp|code asset/i.test(l));
    expect(bad, "every bootstrap stage reused / no changes").toEqual([]);
    expect(summary).toMatch(/no changes/i);
    expect(summary).toMatch(/0 created/);
  } else {
    testInfo.annotations.push({ type: "pending", description: "bootstrap re-run summary not supplied (SCENARIO_BOOTSTRAP_SUMMARY) — idempotency half pending" });
    ledger("s09 bootstrap summary not supplied — health/capabilities half PASS, idempotency half PENDING");
  }
  stateSet("go_live", { hr_version: Number(hrRow.version), runtime_version: Number(rtRow.version) });
  ledger(`s09 PASS hr v${hrRow.version} runtime v${rtRow.version}`);
});

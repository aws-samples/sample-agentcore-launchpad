import { expect, test } from "@playwright/test";
import { chatOnce, EVIDENCE_DIR, getAgent, ledger, listAgents, loginFresh, looksLikeError, memoryRailEvents, sleep } from "../helpers";
import { softly, CI_EXPECT, CI_QUESTION, GATEWAY_TOOL, KB_EXPECT, KB_QUESTION, mask, MEMORY_FACTS, record, SKILL_QUESTION, snap, stateGet, stateRequire, stateSet, stepGate, TOKENS, TOOL_QUESTION } from "./scn";

/**
 * s05 — use the HR assistant (plan §3 step 5; REQUIREMENTS §4.1 cases 2, 5, 6, 7, 8, 12, 14, 15, 16).
 *
 * Every capability mounted in s04 is exercised through the Chat console on the HR assistant,
 * one session per capability (the sessions become the evaluation dataset in s07):
 *   1 facts (memory)  2 KB (kb_search)  3 gateway tool  4 A2A peer  5 Code Interpreter  6 skill
 *   7 hotline code (the Chat ↔ /v1 parity probe)
 * then long-term memory recall in a new session, the public /v1 API with an API key, three
 * sessions on the runtime twin (the canary's online evaluation needs traces) and the member's ownership
 * boundary (Users module) with the second identity from s00.
 */
test.describe.configure({ mode: "serial" });

const HOTLINE_PROMPT = "What is the HR hotline code? Reply with the code and nothing else.";

test("s05a — seven Chat sessions on the HR assistant: memory rail, kb_search, hr-database tool, payroll A2A, Code Interpreter, skill, hotline code", async ({ page }, testInfo) => {
  stepGate("s05");
  test.setTimeout(45 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  const payroll = stateRequire("payroll", "s04");
  const sessions: string[] = [];
  const turn = async (label: string, prompt: string, timeoutMs = 5 * 60 * 1000) => {
    await page.goto(`/v2/chat?agent=${encodeURIComponent(hr.id)}`); // a fresh session per capability
    const r = await chatOnce(page, hr.id, prompt, { timeoutMs });
    sessions.push(r.sessionId);
    await record(testInfo, `${label}.txt`, `${r.answer}\n\ntools: ${r.toolNames.join(", ")}`, `${r.seconds}s errors=${r.errors} tools=${r.toolNames.join(",")} answer="${r.answer.slice(0, 120).replace(/\n/g, " ")}"`);
    await snap(page, `s05-${label}`, undefined, { key: ["kb", "a2a", "code-interpreter"].includes(label) });
    expect(r.errors, `${label}: no error bubble`).toBe(0);
    expect(looksLikeError(r.answer), `${label}: model text, not echo/error`).toBe(false);
    return r;
  };

  // 1 — facts + memory rail without reload (case 2)
  await page.goto(`/v2/chat?agent=${encodeURIComponent(hr.id)}`);
  await expect(page.getByTestId("chat-memory-state")).toHaveAttribute("data-state", "on");
  const facts = await turn("facts", `Please remember two things about me: my manager is ${MEMORY_FACTS.manager} and my cost center is ${MEMORY_FACTS.costCenter}.`);
  const urlBefore = page.url();
  const railEvents = await memoryRailEvents(page, 90_000);
  expect(page.url(), "no navigation while waiting for the rail").toBe(urlBefore);
  expect(railEvents, "memory rail ≥ 1 short-term event without reload").toBeGreaterThanOrEqual(1);
  stateSet("sessions", { hr: [...sessions], runtime: [], facts: facts.sessionId });

  // 2 — knowledge base (case 7)
  const kb = await turn("kb", KB_QUESTION);
  // the Harness mounts two KB tools (kb_search + kb_deep_search, bb6caf37); run #8 was the first time GLM picked the deep one
  expect(kb.toolNames.join(" "), "a KB tool call (kb_search / kb_deep_search) rendered").toMatch(/kb_(deep_)?search/i);
  expect(kb.answer, "answer quotes the 45-day filing window").toMatch(KB_EXPECT);
  expect(kb.answer, "answer names the line manager as approver").toMatch(/line manager/i);

  // 3 — gateway tool (case 5)
  const tool = await turn("tool", TOOL_QUESTION);
  expect(tool.toolCalls, "≥ 1 gateway tool call").toBeGreaterThanOrEqual(1);
  // a tool of the hr-database gateway family (the model may add list_departments etc.); the facts below stay hard
  expect(tool.toolNames.join(" "), `an hr-database gateway tool was called (got ${tool.toolNames.join(",")})`).toMatch(/hr-database___/);
  expect(tool.answer).toMatch(/Maya Chen/);
  expect(tool.answer).toMatch(/HR Operations/);

  // 4 — A2A peer (case 16)
  const a2a = await turn("a2a", "What is the net pay for a gross salary of 10000? Ask the payroll specialist and quote its answer word for word.", 6 * 60 * 1000);
  const slug = "ask_" + payroll.name.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  expect(a2a.toolNames.some((t) => t.startsWith("ask_")), `ask_<payroll> tool call (expected ${slug}; got ${a2a.toolNames.join(",")})`).toBe(true);
  expect(a2a.answer, "no relayed A2A failure").not.toMatch(/returned no text|\[a2a error\]|task failed/i);
  if (!a2a.answer.includes(TOKENS.payroll)) ledger(`soft: A2A relay answered without the payroll token: ${a2a.answer.slice(0, 120)}`);
  softly(testInfo, "answer carries the payroll token (soft — model instruction)", () => expect(a2a.answer, "answer carries the payroll token (soft — model instruction)").toContain(TOKENS.payroll));

  // 5 — Code Interpreter (case 15)
  const ci = await turn("code-interpreter", CI_QUESTION);
  // the exact integer is the fact (hard, below); whether the model shows a tool call is its behaviour (soft)
  if (ci.toolCalls < 1) ledger(`soft: code-interpreter turn answered without a visible tool call (tools=${ci.toolNames.join(",")})`);
  softly(testInfo, "tool call visible (soft)", () => expect(ci.toolCalls, "tool call visible (soft)").toBeGreaterThanOrEqual(1));
  softly(testInfo, "a code/interpreter tool (soft)", () => expect(ci.toolNames.join(" "), "a code/interpreter tool (soft)").toMatch(/code|interpreter|execute|python/i));
  expect(ci.answer.replace(/[,\s]/g, "")).toMatch(new RegExp(CI_EXPECT));

  // 6 — skill (case 8)
  const skill = await turn("skill", SKILL_QUESTION, 6 * 60 * 1000);
  expect(skill.answer).not.toMatch(/maximum token|MaxTokensReached|stopped generating/i);
  expect(skill.answer).toMatch(/EMP-1024/);
  expect(skill.answer).toMatch(/travel|lodging|meals/i);
  expect(skill.answer).toMatch(/1[,.]?068/);

  // 7 — hotline code (parity probe reused by s05c and s09)
  const hotline = await turn("hotline", HOTLINE_PROMPT);
  if (!new RegExp(TOKENS.hotline, "i").test(hotline.answer)) ledger(`soft: hotline turn without the code: ${hotline.answer.slice(0, 120)}`);
  softly(testInfo, "hotline code (soft — model instruction)", () => expect(hotline.answer, "hotline code (soft — model instruction)").toMatch(new RegExp(TOKENS.hotline, "i")));

  stateSet("sessions", { hr: [...sessions], runtime: [], facts: facts.sessionId });
  ledger(`s05a PASS ${sessions.length} sessions on ${hr.name}: ${sessions.map((s) => s.slice(0, 8)).join(",")}`);
});

test("s05b — long-term memory: ≥ 2 records ≤ 10 min after the facts session; a new session recalls 2/2", async ({ page }, testInfo) => {
  stepGate("s05");
  test.setTimeout(25 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  const sessions = stateRequire("sessions", "s05");
  const factsSession = sessions.facts!;
  const mem = (await (await page.request.get(`/api/chat/${hr.id}/memory?session_id=${encodeURIComponent(factsSession)}`, { timeout: 60_000 })).json()) as { actor_id: string; records?: unknown[] };
  const actorId = mem.actor_id;

  const tWait = Date.now();
  let records: unknown[] = mem.records ?? [];
  while (records.length < 2 && Date.now() - tWait < 10 * 60 * 1000) {
    await sleep(10_000);
    const s = (await (await page.request.get(`/api/chat/${hr.id}/memory?session_id=${encodeURIComponent(factsSession)}`, { timeout: 60_000 })).json()) as { records?: unknown[] };
    records = s.records ?? [];
    if (Math.round((Date.now() - tWait) / 1000) % 60 < 10) ledger(`s05b waiting for long-term records… ${Math.round((Date.now() - tWait) / 1000)}s records=${records.length}`);
  }
  const extractSeconds = Math.round((Date.now() - tWait) / 1000);
  await record(testInfo, "long-term-records.json", records, `${records.length} records after ${extractSeconds}s`);
  await page.goto(`/v2/memory?tab=long-term&actor=${encodeURIComponent(actorId)}`);
  await expect(page.getByTestId("v2-memory-longterm")).toBeVisible({ timeout: 60_000 });
  await snap(page, "s05b-memory-long-term", `${records.length} records`);
  expect(records.length, "≥ 2 long-term records within 10 min").toBeGreaterThanOrEqual(2);
  expect(extractSeconds).toBeLessThanOrEqual(600);

  await page.goto(`/v2/chat?agent=${encodeURIComponent(hr.id)}`);
  const recall = await chatOnce(page, hr.id, "Who is my manager, and what is my cost center? Answer with both.");
  await record(testInfo, "recall.txt", recall.answer, `${recall.seconds}s session=${recall.sessionId.slice(0, 8)}`);
  await snap(page, "s05b-recall", undefined, { key: true });
  expect(recall.sessionId).not.toBe(factsSession);
  const hits = [new RegExp(MEMORY_FACTS.manager, "i").test(recall.answer), new RegExp(MEMORY_FACTS.costCenter, "i").test(recall.answer)].filter(Boolean).length;
  expect(hits, "new session recalls ≥ 1 of the 2 facts (memory works)").toBeGreaterThanOrEqual(1);
  if (hits < 2) ledger(`soft: recall ${hits}/2 facts: ${recall.answer.slice(0, 160)}`);
  softly(testInfo, "new session recalls 2/2 facts (soft — model recall)", () => expect(hits, "new session recalls 2/2 facts (soft — model recall)").toBe(2));
  stateSet("sessions", { ...sessions, hr: [...sessions.hr, recall.sessionId], recall: recall.sessionId });
  ledger(`s05b PASS records=${records.length} extract=${extractSeconds}s recall=${hits}/2`);
});

test("s05c — public /v1 API: key → invoke 200 with the hotline code (same answer path as Chat) → no/bad key 401 → disabled key 401", async ({ page, request }, testInfo) => {
  stepGate("s05");
  test.setTimeout(20 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  await page.goto(`/v2/chat?agent=${encodeURIComponent(hr.id)}`);
  await page.getByRole("tab", { name: /API/i }).click();
  await expect(page.getByTestId("api-panel")).toBeVisible();
  // API keys are workspace-wide (routers/apikeys.py) and outlive their agents (N-SCN-obs4): keys of earlier runs list
  // here too, so wait for the list to load and assert the NEW row by prefix instead of counting rows
  const keysRes = await page.request.get("/api/apikeys", { timeout: 60_000 });
  expect(keysRes.ok()).toBeTruthy();
  const keysBefore = (await keysRes.json()) as { keys?: unknown[]; items?: unknown[] };
  const beforeCount = (keysBefore.keys ?? keysBefore.items ?? []).length;
  await expect(page.getByTestId("api-key-row")).toHaveCount(beforeCount, { timeout: 30_000 });
  await page.getByTestId("new-key").click();
  await expect(page.getByTestId("new-key-value")).toBeVisible({ timeout: 30_000 });
  const key = (await page.getByTestId("new-key-value").innerText()).trim();
  expect(key).toMatch(/^lp_live_[0-9a-f]{32}$/);
  await expect(page.getByTestId("api-key-row").filter({ hasText: key.slice(0, 12) }), "the new key row is listed").toHaveCount(1, { timeout: 30_000 });
  await page.screenshot({ path: `${EVIDENCE_DIR}/s05c-api-key-created.png`, fullPage: true, mask: [page.getByTestId("new-key-value")] });

  const v1 = await request.post(`/v1/agents/${hr.id}/invoke`, { headers: { "X-Api-Key": key }, data: { prompt: HOTLINE_PROMPT }, timeout: 5 * 60 * 1000 });
  const v1Body = (await v1.json().catch(async () => ({ raw: await v1.text() }))) as { text?: string; session_id?: string };
  await record(testInfo, "v1-invoke.json", v1Body, `/v1 → ${v1.status()} ${mask(JSON.stringify(v1Body)).slice(0, 160)}`);
  expect(v1.status()).toBe(200);
  expect(looksLikeError(String(v1Body.text))).toBe(false);
  if (!new RegExp(TOKENS.hotline, "i").test(String(v1Body.text))) ledger(`soft: /v1 answer without the hotline code: ${String(v1Body.text).slice(0, 120)}`);
  softly(testInfo, "/v1 answer carries the hotline code (soft — model instruction)", () => expect(String(v1Body.text), "/v1 answer carries the hotline code (soft — model instruction)").toMatch(new RegExp(TOKENS.hotline, "i")));
  expect(v1Body.session_id).toBeTruthy();
  // observation N-SCN-obs1 (dry run 2026-10-11): the sync /v1 invoke mints a session id but the console's session list only
  // shows sessions with a persisted ChatMessage transcript — the /v1 session is therefore NOT listed. Recorded, not asserted
  // (case 14 only ledgered this too); the "one invoke chain" proof is the identical answer below.
  const listed = (await (await page.request.get(`/api/chat/${hr.id}/sessions`, { timeout: 60_000 })).json()) as { sessions: Array<{ session_id: string }> };
  const v1Listed = listed.sessions.some((s) => s.session_id === v1Body.session_id);
  ledger(`s05c /v1 session listed with the console sessions = ${v1Listed} (N-SCN-obs1)`);
  if (!v1Listed) testInfo.annotations.push({ type: "observation", description: "N-SCN-obs1: /v1 session not listed in /api/chat/{id}/sessions (no persisted transcript)" });
  const agents = await request.get("/v1/agents", { headers: { "X-Api-Key": key } });
  expect(agents.status()).toBe(200);
  expect(((await agents.json()).agents as Array<{ id: string }>).some((a) => a.id === hr.id)).toBe(true);
  expect((await request.post(`/v1/agents/${hr.id}/invoke`, { data: { prompt: HOTLINE_PROMPT } })).status(), "no key → 401").toBe(401);
  expect((await request.post(`/v1/agents/${hr.id}/invoke`, { headers: { "X-Api-Key": "lp_live_deadbeef" }, data: { prompt: HOTLINE_PROMPT } })).status(), "bad key → 401").toBe(401);

  await page.goto(`/v2/chat?agent=${encodeURIComponent(hr.id)}`);
  await page.getByRole("tab", { name: /API/i }).click();
  const row = page.getByTestId("api-key-row").filter({ hasText: key.slice(0, 12) }).first();
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: /disable/i }).click();
  await page.getByTestId("v2-confirm-ok").click();
  await expect(row.getByText(/disabled/i)).toBeVisible({ timeout: 30_000 });
  await snap(page, "s05c-api-key-disabled", undefined, { key: true });
  const after = await request.post(`/v1/agents/${hr.id}/invoke`, { headers: { "X-Api-Key": key }, data: { prompt: HOTLINE_PROMPT } });
  expect(after.status(), "disabled key → 401").toBe(401);
  const sessions = stateRequire("sessions", "s05");
  stateSet("sessions", { ...sessions, hr: [...sessions.hr, String(v1Body.session_id)] });
  ledger(`s05c PASS /v1 session=${String(v1Body.session_id).slice(0, 8)}`);
});

test("s05d — three Chat sessions on the HR runtime twin (magic word, KB, tool) so it has traces for the canary", async ({ page }, testInfo) => {
  stepGate("s05");
  test.setTimeout(30 * 60 * 1000);
  const runtime = stateRequire("runtime", "s04");
  const prompts = ["What is the magic word? Reply with it only.", KB_QUESTION, TOOL_QUESTION];
  const ids: string[] = [];
  for (const [i, prompt] of prompts.entries()) {
    await page.goto(`/v2/chat?agent=${encodeURIComponent(runtime.id)}`);
    const r = await chatOnce(page, runtime.id, prompt);
    ids.push(r.sessionId);
    await record(testInfo, `runtime-${i + 1}.txt`, `${r.answer}\n\ntools: ${r.toolNames.join(", ")}`, `${r.seconds}s errors=${r.errors} tools=${r.toolNames.join(",")}`);
    expect(r.errors, `runtime turn ${i + 1}: no error bubble`).toBe(0);
    expect(looksLikeError(r.answer), `runtime turn ${i + 1}: model text`).toBe(false);
    if (i === 0) expect(r.answer).toMatch(new RegExp(TOKENS.runtimeMagic, "i"));
    if (i === 1) softly(testInfo, "zip runtime answers the KB question from kb_search", () => expect(r.answer).toMatch(KB_EXPECT));
    if (i === 2) softly(testInfo, "zip runtime calls the gateway tool", () => expect(r.toolNames.join(" ")).toMatch(new RegExp(GATEWAY_TOOL)));
  }
  await snap(page, "s05d-runtime-chat");
  expect(new Set(ids).size).toBe(3);
  const sessions = stateRequire("sessions", "s05");
  stateSet("sessions", { ...sessions, runtime: ids });
  ledger(`s05d PASS runtime sessions=${ids.map((s) => s.slice(0, 8)).join(",")}`);
});

test("s05e — ownership boundary with the member identity: no Users page, /api/users 403, DELETE / redeploy of the admin's HR assistant → 403, agent untouched", async ({ page, browser }, testInfo) => {
  stepGate("s05");
  test.setTimeout(15 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  const member = stateRequire("member", "s00");
  const baseURL = testInfo.project.use.baseURL as string;
  const { page: mp, context: mc } = await loginFresh(browser, member.username, member.password, baseURL);
  const mstatus = (await (await mp.request.get("/api/auth/status", { timeout: 60_000 })).json()) as { role?: string };
  expect(mstatus.role).toBe("member");
  await mp.goto("/v2");
  await expect(mp.getByTestId("v2-shell")).toBeVisible();
  const hrefs = await mp.locator("aside.v2-side a[href]").evaluateAll((as) => as.map((a) => (a as HTMLAnchorElement).getAttribute("href") ?? ""));
  expect(hrefs.some((h) => h.startsWith("/v2/users")), "member must not see the Users page").toBe(false);
  expect((await mp.request.get("/api/users", { timeout: 60_000 })).status(), "GET /api/users as member").toBe(403);
  const victim = await getAgent(page.request, hr.id);
  const del = await mp.request.delete(`/api/agents/${hr.id}`);
  const delBody = (await del.json().catch(async () => ({ raw: await del.text() }))) as { code?: string };
  const redeploy = await mp.request.post(`/api/agents/${hr.id}/redeploy`, { data: victim.spec });
  await record(testInfo, "member-mutations.json", { delete: { status: del.status(), body: delBody }, redeploy: redeploy.status() }, `member DELETE → ${del.status()} redeploy → ${redeploy.status()}`);
  await mp.goto(`/v2/agents?view=detail&id=${encodeURIComponent(hr.id)}`);
  await expect(mp.getByTestId("v2-agent-basic")).toBeVisible({ timeout: 30_000 });
  await mp.screenshot({ path: `${EVIDENCE_DIR}/s05e-member-agent-detail.png`, fullPage: true });
  await mc.close();
  expect(del.status(), "member deleting the admin's agent → 403").toBe(403);
  expect(String(delBody.code ?? "")).toBe("agent.forbidden");
  expect(redeploy.status(), "member redeploying the admin's agent → 403").toBe(403);
  expect((await listAgents(page.request)).some((a) => a.id === hr.id && a.status === "active"), "HR assistant still active").toBe(true);
  const sessionsBefore = stateGet("sessions");
  ledger(`s05e PASS member=${member.username} hr sessions so far=${sessionsBefore?.hr.length ?? 0}`);
});

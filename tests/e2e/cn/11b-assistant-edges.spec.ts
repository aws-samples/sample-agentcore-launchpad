import { expect, test } from "@playwright/test";
import { attach, E2E_PREFIX, evidence, ledger, loginFresh, setLanguage, stateGet, stateSet } from "./helpers";

/**
 * arch-e2e edge paths of the architect assistant (2026-10-12), through the UI/API only:
 *
 *  arch-4a  an out-of-scope / nonsense brief → the assistant asks a clarifying question and
 *           emits NO proposal.
 *  arch-4b  a brief that demands a model outside the CN catalog (GLM 5.3 / Kimi K3) → either the
 *           assistant refuses, or the server marks the proposal `invalid` with a readable
 *           validation error; a direct PUT of such a proposal and an approve attempt are refused.
 *  arch-4c  a freshly registered + approved member opens /v2/assistant: the status, the page
 *           and a conversation create are recorded with their HTTP codes; the member cannot read
 *           the admin's conversation (404 — never 403).
 *  arch-4d  zh-CN: the assistant converses in Chinese and the proposal card renders in Chinese;
 *           the proposal is rejected (nothing deployed).
 */

test.describe.configure({ mode: "serial" });

const ALLOWED = ["global.zai.glm-5.3", "global.moonshotai.kimi-k3"];

function rememberConversation(id: string | null): void {
  if (!id) return;
  const cur = stateGet<string[]>("arch_conversations") ?? [];
  if (!cur.includes(id)) stateSet("arch_conversations", [...cur, id]);
}

async function newConversation(page: import("@playwright/test").Page): Promise<string> {
  await page.goto("/v2/assistant");
  await page.getByTestId("v2-assistant-new").click();
  await expect(page.getByTestId("v2-assistant-detail")).toBeVisible({ timeout: 60_000 });
  const id = await page.getByTestId("v2-assistant-detail").getAttribute("data-conversation");
  rememberConversation(id);
  return id!;
}

async function sendTurn(page: import("@playwright/test").Page, prompt: string): Promise<{ seconds: number; errors: number; reply: string }> {
  const before = (await page.getByTestId("v2-assistant-thread").innerText()).length;
  const t0 = Date.now();
  await page.getByTestId("v2-assistant-input").fill(prompt);
  await page.getByTestId("v2-assistant-send").click();
  await expect(page.getByTestId("v2-assistant-streaming")).toHaveCount(0, { timeout: 8 * 60 * 1000 });
  await expect(page.getByTestId("v2-assistant-input")).toBeEnabled({ timeout: 60_000 });
  const errors = await page.getByTestId("v2-assistant-turn-error").count();
  const thread = await page.getByTestId("v2-assistant-thread").innerText();
  return { seconds: Math.round((Date.now() - t0) / 1000), errors, reply: thread.slice(before) };
}

test("arch-4a: out-of-scope brief → clarifying question, no proposal", async ({ page }, testInfo) => {
  ledger("arch-4a start");
  await setLanguage(page, "en");
  const id = await newConversation(page);
  const r = await sendTurn(page, "banana telescope 42 — also, can you book me a flight to the moon and tell me tomorrow's lottery numbers?");
  const conv = await (await page.request.get(`/api/assistant/architect/conversations/${id}`)).json();
  const proposalCard = await page.getByTestId("v2-assistant-proposal-view").count();
  const asksQuestion = /\?|？/.test(r.reply);
  ledger(`arch-4a conversation=${id} ${r.seconds}s errors=${r.errors} proposal_status=${conv.proposal_status ?? "none"} proposals=${(conv.proposals ?? []).length} card=${proposalCard} asks-question=${asksQuestion}: ${r.reply.slice(0, 400).replace(/\n/g, " ")}`);
  await attach(testInfo, "conversation-4a.json", conv);
  await evidence(page, "arch4a-out-of-scope");
  expect(r.errors).toBe(0);
  expect((conv.proposals ?? []).length, "no proposal for a nonsense brief").toBe(0);
  expect(proposalCard).toBe(0);
  expect(asksQuestion, "the assistant asks a clarifying question").toBe(true);
  ledger("arch-4a end PASS");
});

test("arch-4b: a model outside the GLM/Kimi catalog is refused server-side with a readable message", async ({ page }, testInfo) => {
  ledger("arch-4b start");
  await setLanguage(page, "en");
  const id = await newConversation(page);
  const stamp = new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "");
  const name = `${E2E_PREFIX}badmodel-${stamp}`;
  const r = await sendTurn(page, `Build a tiny FAQ agent named ${name} that answers questions about Octank's office hours (9:00-18:00 CST, Mon-Fri) in one sentence. No tools, no knowledge base, no memory. Hard customer requirement: the model_id MUST be "anthropic.claude-sonnet-4-5" — do not substitute another model. Everything is final; write the proposal block now.`);
  let conv = await (await page.request.get(`/api/assistant/architect/conversations/${id}`)).json();
  const proposals = (conv.proposals ?? []) as Array<{ revision: number; status: string; content: Record<string, unknown>; validation_errors?: string[] }>;
  const latest = proposals[proposals.length - 1];
  const viaModel = { status: latest?.status ?? "none", model_id: latest?.content?.model_id, validation_errors: latest?.validation_errors ?? [] };
  const cardText = (await page.getByTestId("v2-assistant-proposal").innerText().catch(() => "")) ?? "";
  ledger(`arch-4b model turn ${r.seconds}s errors=${r.errors} proposal_status=${conv.proposal_status ?? "none"} latest=${JSON.stringify(viaModel).slice(0, 500)}: ${r.reply.slice(0, 400).replace(/\n/g, " ")}`);
  await evidence(page, "arch4b-model-turn");
  await attach(testInfo, "conversation-4b.json", conv);
  // whatever the model did, the platform must never hold an APPROVABLE proposal with a foreign model
  if (latest) {
    const foreign = !ALLOWED.includes(String(latest.content?.model_id));
    ledger(`arch-4b model's proposal: foreign-model=${foreign} status=${latest.status}`);
    if (foreign) expect(latest.status, "a foreign-model proposal is marked invalid").toBe("invalid");
  }

  // the server-side rule, probed directly: a member edit naming a foreign model. The content is
  // MINIMAL (no evaluation plan / golden tests) so the only possible error is the model: when the
  // model's own proposal also carried an evaluation_plan error, only THAT error was listed —
  // validation stops at the first failing stage (recorded as an observation, not asserted here).
  const modelTurnErrors = ((conv.messages ?? []) as Array<{ role: string; text: string }>).filter((m) => m.role === "error").map((m) => String(m.text).slice(0, 300));
  ledger(`arch-4b model-turn error bubbles=${JSON.stringify(modelTurnErrors)}`);
  const edited = {
    version: 1, name, model_id: "anthropic.claude-sonnet-4-5", model_source: "bedrock",
    system_prompt: "Answer questions about Octank's office hours (9:00-18:00 CST, Monday to Friday) in one sentence.",
    tools: [], skills: [], knowledge_bases: [], memory: "disabled",
  };
  const put = await page.request.put(`/api/assistant/architect/conversations/${id}/proposal`, { data: { content: edited } });
  const putBody = await put.json().catch(() => ({}));
  const putStatus = putBody.proposal?.status;
  const putErrors = (putBody.proposal?.validation_errors ?? []) as string[];
  ledger(`arch-4b PUT foreign model → ${put.status()} status=${putStatus ?? putBody.code} errors=${JSON.stringify(putErrors).slice(0, 400)} ${putBody.message ?? ""}`);
  await attach(testInfo, "put-foreign-model.json", { status: put.status(), body: putBody });
  await page.reload();
  await expect(page.getByTestId("v2-assistant-detail")).toBeVisible({ timeout: 60_000 });
  await evidence(page, "arch4b-invalid-card");
  const readable = [...putErrors, String(putBody.message ?? "")].join(" ");
  expect(put.status() === 200 ? putStatus : "rejected", "foreign model never yields an approvable revision").not.toBe("draft");
  expect(readable, "a readable model message naming the allowed models").toMatch(/model.not_allowed|not available in this build|allowed: global\.zai\.glm-5\.3/i);
  // and an approval of that revision is refused
  if (put.status() === 200 && putBody.proposal) {
    const ap = await page.request.post(`/api/assistant/architect/conversations/${id}/proposal/approve`, { data: { revision: putBody.proposal.revision, content_hash: putBody.proposal.content_hash } });
    const apBody = await ap.json().catch(() => ({}));
    ledger(`arch-4b approve invalid revision → ${ap.status()} ${apBody.code ?? ""} ${String(apBody.message ?? "").slice(0, 200)}`);
    await attach(testInfo, "approve-invalid.json", { status: ap.status(), body: apBody });
    expect(ap.status(), "approve of an invalid revision is refused").toBeGreaterThanOrEqual(400);
  }
  conv = await (await page.request.get(`/api/assistant/architect/conversations/${id}`)).json();
  stateSet("arch_4b", { model_turn: viaModel, put: { status: put.status(), proposal_status: putStatus, errors: putErrors }, card: cardText.slice(0, 200) });
  ledger("arch-4b end PASS");
});

test("arch-4c: member (registered + approved) opens /v2/assistant — behaviour and HTTP codes recorded", async ({ page, browser }, testInfo) => {
  const baseURL = test.info().project.use.baseURL as string;
  const stamp = new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "");
  const username = `${E2E_PREFIX}member-${stamp}`;
  const password = `E2ea!${stamp}-pw-${Math.random().toString(36).slice(2, 8)}`; // throwaway, never logged
  ledger(`arch-4c start member=${username}`);
  await setLanguage(page, "en");
  // register through the sign-in page (fresh context)
  const reg = await browser.newContext({ storageState: { cookies: [], origins: [] }, ignoreHTTPSErrors: true, baseURL, locale: "en-US", viewport: { width: 1440, height: 900 } });
  const rp = await reg.newPage();
  await rp.goto("/");
  await rp.getByTestId("auth-tab-register").click();
  await rp.locator('input[autocomplete="username"]').fill(username);
  await rp.locator('input[autocomplete="email"]').fill(`${username}@example.invalid`);
  const pws = rp.locator('input[autocomplete="new-password"]');
  await pws.nth(0).fill(password);
  await pws.nth(1).fill(password);
  const regRes = rp.waitForResponse((r) => r.url().includes("/api/auth/register"));
  await pws.nth(1).press("Enter");
  const rr = await regRes;
  const regBody = await rr.json();
  ledger(`arch-4c register → ${rr.status()} status=${regBody.status}`);
  await expect(rp.getByTestId("register-success")).toBeVisible({ timeout: 30_000 });
  await reg.close();
  expect(rr.status()).toBe(201);
  // admin approves in the Users page
  await page.goto("/v2/users");
  await expect(page.getByTestId("v2-users-table")).toBeVisible();
  await page.getByTestId(`v2-users-approve-${username}`).click();
  await expect(page.getByTestId("v2-users-approve")).toBeVisible();
  const chip = page.getByTestId("v2-users-approve-ws-default");
  if (await chip.isVisible().catch(() => false)) {
    const on = (await chip.getAttribute("aria-pressed")) === "true" || /\bon\b/.test((await chip.getAttribute("class")) ?? "");
    if (!on) await chip.click();
  }
  const patchRes = page.waitForResponse((r) => r.url().includes("/api/users/") && r.request().method() === "PATCH");
  await page.getByTestId("v2-users-approve-ok").click();
  const pr = await patchRes;
  const member = await pr.json();
  ledger(`arch-4c approve → ${pr.status()} status=${member.status} role=${member.role} permissions=${JSON.stringify(member.permissions ?? {}).slice(0, 200)}`);
  expect(pr.status()).toBe(200);
  stateSet("arch_member", { username, id: member.id });
  await evidence(page, "arch4c-member-approved");

  // member session
  const { page: mp, context: mc } = await loginFresh(browser, username, password, baseURL);
  const st = await mp.request.get("/api/assistant/architect");
  const stBody = await st.json().catch(() => ({}));
  ledger(`arch-4c member GET /api/assistant/architect → ${st.status()} available=${stBody.available} can_deploy=${stBody.can_deploy} deploy_requirements=${JSON.stringify(stBody.deploy_requirements)} is_admin=${stBody.is_admin}`);
  await attach(testInfo, "member-assistant-status.json", { status: st.status(), body: stBody });
  await mp.goto("/v2/assistant");
  // the shell re-mounts behind the auth spinner on a full navigation — wait for it before sampling
  await expect(mp.locator("aside[aria-label]")).toBeVisible({ timeout: 60_000 });
  await mp.waitForTimeout(3000);
  const navShown = await mp.getByTestId("v2-nav-/v2/assistant").isVisible().catch(() => false);
  const newBtn = await mp.getByTestId("v2-assistant-new").isVisible({ timeout: 15_000 }).catch(() => false);
  const unavailable = await mp.getByTestId("v2-assistant-unavailable").count();
  const pageText = ((await mp.locator(".v2-main, main, body").first().innerText().catch(() => "")) ?? "").replace(/\s+/g, " ").slice(0, 600);
  ledger(`arch-4c member page text: ${pageText}`);
  await evidence(mp, "arch4c-member-assistant-page");
  const lst = await mp.request.get("/api/assistant/architect/conversations");
  const lstBody = await lst.json().catch(() => ({}));
  const adminConvs = stateGet<string[]>("arch_conversations") ?? [];
  const foreign = adminConvs[0] ? await mp.request.get(`/api/assistant/architect/conversations/${adminConvs[0]}`) : null;
  const create = await mp.request.post("/api/assistant/architect/conversations", { data: { title: `${E2E_PREFIX}member-probe` } });
  const createBody = await create.json().catch(() => ({}));
  if (create.ok() && createBody.id) rememberConversation(String(createBody.id));
  ledger(`arch-4c member: nav=${navShown} new-button=${newBtn} unavailable-notice=${unavailable} list → ${lst.status()} (${(lstBody.conversations ?? []).length} visible) admin-conversation → ${foreign?.status() ?? "n/a"} create → ${create.status()} ${createBody.code ?? createBody.id ?? ""}`);
  await attach(testInfo, "member-assistant-probe.json", { status: st.status(), list: lst.status(), visible: (lstBody.conversations ?? []).length, foreign: foreign?.status(), create: create.status(), createBody });
  stateSet("arch_4c", { status: st.status(), can_deploy: stBody.can_deploy, nav: navShown, newBtn, unavailable, list: lst.status(), foreign: foreign?.status(), create: create.status() });
  if (foreign) expect(foreign.status(), "another principal's conversation is 404 (never 403)").toBe(404);
  expect([200, 403]).toContain(st.status());
  await mc.close();
  ledger("arch-4c end PASS");
});

test("arch-4d: zh-CN — the assistant converses in Chinese and the proposal card renders in Chinese", async ({ page }, testInfo) => {
  ledger("arch-4d start");
  await setLanguage(page, "zh-CN");
  const id = await newConversation(page);
  const stamp = new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "");
  const name = `${E2E_PREFIX}zh-${stamp}`;
  const prompts = [
    `请为虚构公司 Octank 设计一个内部 IT 服务台智能体，名称 ${name}。职责：用中文简要回答员工的 IT 问题（VPN、笔记本、密码重置、入职账号），无法解决时建议联系 it-support@octank.example。不需要工具、知识库和记忆，使用默认的 GLM 模型。所有决定均已确认，请直接给出 proposal。`,
    `是的，以上全部确认：名称 ${name}，无工具、无知识库、无记忆，默认 GLM 模型。请现在输出 proposal 块。`,
    `请按你的最佳假设直接输出 proposal 块。`,
  ];
  const fixPrompt = "请用 patch 块修正校验错误；保留已确认的决定，去掉工作区无法提供的内容。";
  const proposal = page.getByTestId("v2-assistant-proposal-view");
  const statuses: string[] = [];
  let turns = 0;
  let reply = "";
  let conv: Record<string, unknown> = {};
  for (let i = 0; i < prompts.length; i++) {
    turns += 1;
    const p = statuses[statuses.length - 1] === "invalid" ? fixPrompt : prompts[i];
    const r = await sendTurn(page, p);
    reply = r.reply;
    conv = await (await page.request.get(`/api/assistant/architect/conversations/${id}`)).json();
    const st = String(conv.proposal_status ?? "none");
    statuses.push(st);
    ledger(`arch-4d turn ${turns} ${r.seconds}s errors=${r.errors} proposal_status=${st}: ${reply.slice(0, 300).replace(/\n/g, " ")}`);
    await evidence(page, `arch4d-turn-${turns}`);
    if ((await proposal.isVisible().catch(() => false)) && st !== "invalid") break;
  }
  await attach(testInfo, "conversation-4d.json", conv);
  const cjk = /[一-鿿]/;
  const assistantTexts = ((conv.messages ?? []) as Array<{ role: string; text: string }>).filter((m) => m.role === "assistant").map((m) => String(m.text ?? "").replace(/```[\s\S]*?```/g, ""));
  const repliesChinese = assistantTexts.length > 0 && assistantTexts.every((t) => cjk.test(t));
  const cardVisible = await proposal.isVisible().catch(() => false);
  const cardText = cardVisible ? await page.getByTestId("v2-assistant-proposal").innerText() : "";
  const cardChinese = cjk.test(cardText);
  const proposedName = cardVisible ? (await page.getByTestId("v2-assistant-proposal-name").innerText()).trim() : "";
  ledger(`arch-4d turns=${turns} statuses=${statuses.join("→")} replies-chinese=${repliesChinese} card=${cardVisible} card-chinese=${cardChinese} name=${proposedName}`);
  await evidence(page, "arch4d-proposal-card-zh");
  stateSet("arch_4d", { turns, statuses, repliesChinese, cardVisible, cardChinese, proposedName });
  expect(cardVisible, `a proposal must appear within 3 turns (statuses ${statuses.join("→")})`).toBe(true);
  expect(statuses[statuses.length - 1]).not.toBe("invalid");
  expect(cardChinese, "the proposal card renders Chinese labels").toBe(true);
  expect(repliesChinese, "the assistant replies in Chinese").toBe(true);
  // nothing is deployed from this conversation: reject the proposal
  const reject = page.getByTestId("v2-assistant-proposal-reject");
  if (await reject.isVisible().catch(() => false)) {
    const rej = page.waitForResponse((r) => r.url().includes("/proposal/reject") && r.request().method() === "POST");
    await reject.click();
    const ok = page.getByTestId("v2-confirm-ok");
    if (await ok.isVisible({ timeout: 3000 }).catch(() => false)) await ok.click();
    const rr = await rej;
    ledger(`arch-4d reject → ${rr.status()}`);
    await evidence(page, "arch4d-rejected");
  }
  await setLanguage(page, "en");
  ledger("arch-4d end PASS");
});

import { expect, test } from "@playwright/test";
import { ALLOWED, asksAboutMemory, lastAssistantProse, latestProposal, MEMORY_RE, newConversation, QUESTION_RE, rejectIfOffered, sendTurn, SILICONFLOW_SSM, stamp } from "./assistant-helpers";
import { attach, chatOnce, E2E_PREFIX, evidence, getAgent, ledger, looksLikeError, setLanguage, sleep, stateGet, stateSet, waitForJob } from "./helpers";

/**
 * arch-retest (2026-10-12) — the architect preset release 1.6.0-51de43b0c134 (prompt policy +
 * CN memory recognition) on the default environment, through the UI/API only:
 *
 *  rt-1  ask-don't-assume: a brief that leaves long-term memory UNSPECIFIED (and does not say
 *        "decide yourself") must make the architect ASK about memory before any proposal; once
 *        every decision is stated the architect must not re-ask and must propose.
 *  rt-2  validation lists every error at once: a member edit (PUT) with an unknown model id AND
 *        an unknown knowledge base yields BOTH errors in `validation_errors`, both on the card in
 *        the conversation, and the replayed fix request repairs both in one revision.
 *  rt-3  model catalog from the server: `/api/capabilities` offers exactly GLM 5.3 + Kimi K3 as
 *        available and the four `siliconflow.*` rows as not configured; the proposal editor's
 *        model picker mirrors that; asking for a SiliconFlow model is refused naming the SSM
 *        parameter `launchpad-siliconflow-api-key`; asking for "Kimi" yields
 *        `global.moonshotai.kimi-k3`.
 *  rt-4  CN memory: a brief that states `memory: workspace` (shared self-hosted memory) validates
 *        and deploys (≤ 15 min); Chat stores two facts; a NEW session recalls them.
 *
 * Test data is `e2ea-…` (N10_PREFIX=e2ea-); conversation ids go to the shared state for
 * 11z-assistant-cleanup.spec.ts, which also deletes every `e2ea-` agent.
 */

// NOT serial: the cases are independent (rt-3 only *optionally* reads rt-2's editor snapshot), and a
// preset-behaviour failure in one case (run A: rt-1) must not skip the others.
test.describe.configure({ mode: "default" });

test("rt-1: memory left unspecified → the architect ASKS about long-term memory; stated decisions are not re-asked", async ({ page }, testInfo) => {
  ledger("rt-1 start");
  await setLanguage(page, "en");
  const kbs = ((await (await page.request.get("/api/knowledge-bases")).json()).items ?? []) as Array<{ name: string; status: string }>;
  const kb = kbs.find((k) => k.status === "ACTIVE");
  const id = await newConversation(page);
  const name = `${E2E_PREFIX}ask-${stamp()}`;
  // NB: nothing about memory, no "decide yourself", no "write the proposal now"
  const brief = `Design an internal IT helpdesk agent for the fictional company Octank, named ${name}. It answers employees' IT questions (VPN, laptop, password reset, onboarding accounts) in English, concisely, and escalates anything it cannot resolve to it-support@octank.example. ${kb ? `Attach the existing knowledge base "${kb.name}" from the catalog for policy look-ups.` : "No knowledge base."} No gateway tools, no skills. Default GLM model. Target users: about 800 employees, business hours only.`;
  const t1 = await sendTurn(page, id, brief);
  const prose1 = lastAssistantProse(t1.conv);
  const asks = QUESTION_RE.test(prose1);
  const mentionsMemory = MEMORY_RE.test(prose1);
  const memoryQuestion = asksAboutMemory(prose1);
  const proposals1 = ((t1.conv.proposals ?? []) as unknown[]).length;
  ledger(`rt-1 turn 1 ${t1.seconds}s errors=${t1.errors} proposal_status=${t1.status} proposals=${proposals1} asks=${asks} mentions-memory=${mentionsMemory} memory-question=${memoryQuestion}: ${prose1.slice(0, 600).replace(/\n/g, " ")}`);
  await evidence(page, "rt1-turn-1-asks");
  await attach(testInfo, "conversation-rt1-turn1.json", t1.conv);
  stateSet("arch_rt1", { turn1: { seconds: t1.seconds, status: t1.status, asks, mentionsMemory, memoryQuestion } });
  // soft: a preset-behaviour miss here (run A: "memory … set to the safe default and flagged as an
  // assumption", no question) must still let the second half — stated decisions are not re-asked — run
  expect.soft(t1.errors).toBe(0);
  expect.soft(proposals1, "no proposal while decisions are open").toBe(0);
  expect.soft(asks, "the architect asks before proposing").toBe(true);
  expect.soft(memoryQuestion, "the architect asks about long-term memory instead of choosing silently").toBe(true);

  // every decision stated → no re-ask, a proposal follows (≤ 2 further turns, fix loop allowed)
  const answers = `Answers to all your questions, all final: name ${name}; default GLM model (global.zai.glm-5.3); knowledge base ${kb ? `"${kb.name}"` : "none"}; no gateway tools, no skills, no code interpreter; memory: DISABLED (no memory across sessions); English only; escalate to it-support@octank.example; about 800 employees, business hours; no legacy system, no historical data, no compliance constraints beyond "no PII in logs"; accept your library defaults for everything else and record them as assumptions. Please write the proposal block now.`;
  const statuses: string[] = [];
  let reAsked = false;
  let turns = 1;
  let conv = t1.conv;
  for (let i = 0; i < 2; i++) {
    turns += 1;
    const p = statuses[statuses.length - 1] === "invalid"
      ? "Please fix the validation errors with a patch block; keep every confirmed decision."
      : i === 0 ? answers : "Proceed with your best assumptions and output the proposal block.";
    const t = await sendTurn(page, id, p);
    conv = t.conv;
    statuses.push(t.status);
    const prose = lastAssistantProse(conv);
    const memQ = asksAboutMemory(prose);
    if (i === 0) reAsked = memQ;
    ledger(`rt-1 turn ${turns} ${t.seconds}s errors=${t.errors} proposal_status=${t.status} re-asked-memory=${memQ}: ${prose.slice(0, 400).replace(/\n/g, " ")}`);
    await evidence(page, `rt1-turn-${turns}`);
    if (t.status === "draft") break;
  }
  const card = await page.getByTestId("v2-assistant-proposal-view").isVisible().catch(() => false);
  const latest = latestProposal(conv);
  ledger(`rt-1 statuses=${statuses.join("→")} card=${card} memory=${latest?.content?.memory} model=${latest?.content?.model_id}`);
  await attach(testInfo, "conversation-rt1.json", conv);
  stateSet("arch_rt1", { ...(stateGet<Record<string, unknown>>("arch_rt1") ?? {}), statuses, reAsked, memory: latest?.content?.memory });
  expect(reAsked, "memory is not re-asked once the member stated it").toBe(false);
  expect(statuses[statuses.length - 1], `a VALID proposal once every decision is stated (statuses ${statuses.join("→")})`).toBe("draft");
  expect(latest?.content?.memory, "the stated memory decision is honoured").toBe("disabled");
  await rejectIfOffered(page, "rt-1");
  ledger("rt-1 end PASS");
});

test("rt-2: two simultaneous errors (unknown model + unknown knowledge base) are listed together, on the card, and fixed in one revision", async ({ page }, testInfo) => {
  ledger("rt-2 start");
  await setLanguage(page, "en");
  const id = await newConversation(page);
  stateSet("arch_rt2_conversation", id);
  const name = `${E2E_PREFIX}twoerr-${stamp()}`;
  const fakeKb = "KBNOTREAL0000000";
  const edited = {
    version: 1, name, model_id: "anthropic.claude-sonnet-4-5", model_source: "bedrock",
    system_prompt: "Answer questions about Octank's office hours (9:00-18:00 CST, Monday to Friday) in one sentence.",
    tools: [], skills: [], knowledge_bases: [fakeKb], memory: "disabled",
  };
  const put = await page.request.put(`/api/assistant/architect/conversations/${id}/proposal`, { data: { content: edited } });
  const body = await put.json().catch(() => ({}));
  const errors = ((body.proposal?.validation_errors ?? []) as string[]);
  const modelErr = errors.find((e) => /model_id/i.test(e));
  const kbErr = errors.find((e) => /knowledge_bases/i.test(e));
  ledger(`rt-2 PUT → ${put.status()} status=${body.proposal?.status ?? body.code} errors(${errors.length})=${JSON.stringify(errors).slice(0, 700)}`);
  await attach(testInfo, "put-two-errors.json", { status: put.status(), body });
  expect(put.status()).toBe(200);
  expect(body.proposal?.status).toBe("invalid");
  expect(errors.length, "both errors are reported at once").toBeGreaterThanOrEqual(2);
  expect(modelErr, "model error present").toMatch(/not available in this build.*global\.zai\.glm-5\.3.*global\.moonshotai\.kimi-k3/);
  expect(kbErr, "knowledge base error present").toMatch(new RegExp(`${fakeKb}.*not an active managed knowledge base`));
  for (const e of errors) expect(e, "readable (no pydantic dump)").not.toMatch(/pydantic|input_value=|validation error for/i);

  // the card inside the conversation lists both
  await page.reload();
  await expect(page.getByTestId("v2-assistant-proposal-view")).toBeVisible({ timeout: 60_000 });
  const cardStatus = await page.getByTestId("v2-assistant-proposal-view").getAttribute("data-status");
  const countText = (await page.getByTestId("v2-assistant-proposal-error-count").innerText().catch(() => "")) ?? "";
  const cardText = await page.getByTestId("v2-assistant-proposal").innerText();
  const threadText = await page.getByTestId("v2-assistant-thread").innerText();
  ledger(`rt-2 card status=${cardStatus} count="${countText}" card-has-model=${/not available in this build/.test(cardText)} card-has-kb=${cardText.includes(fakeKb)} thread-has-errors=${threadText.includes(fakeKb)}`);
  await evidence(page, "rt2-two-errors-card");
  expect(cardStatus).toBe("invalid");
  expect(countText, "the card announces the issue count").toMatch(/2 issues/);
  expect(cardText).toMatch(/not available in this build/);
  expect(cardText).toContain(fakeKb);

  // the editor's model picker: only the server's catalog, unconfigured rows disabled (rt-3 evidence)
  await page.getByTestId("v2-assistant-proposal-edit").click();
  await expect(page.getByTestId("v2-assistant-proposal-editor")).toBeVisible();
  const options = await page.locator("#v2-assistant-models option").evaluateAll((els) => els.map((o) => ({ value: (o as HTMLOptionElement).value, label: o.textContent ?? "", disabled: (o as HTMLOptionElement).disabled })));
  ledger(`rt-2 editor model options=${JSON.stringify(options)}`);
  await attach(testInfo, "editor-model-options.json", options);
  stateSet("arch_editor_models", options);
  await evidence(page, "rt2-editor-model-picker");
  await page.getByRole("button", { name: /cancel/i }).first().click().catch(() => {});

  // the architect receives the replayed errors and fixes BOTH in one revision
  const kbs = ((await (await page.request.get("/api/knowledge-bases")).json()).items ?? []) as Array<{ name: string; status: string }>;
  const kb = kbs.find((k) => k.status === "ACTIVE");
  const t = await sendTurn(page, id, `Please fix the validation errors of my edited proposal with a patch block, in one revision: use the default GLM model, and ${kb ? `use the existing knowledge base "${kb.name}" from the catalog instead of the unknown id` : "drop the unknown knowledge base"}. Keep everything else (name ${name}, no tools, no memory). Everything is final.`);
  const latest = latestProposal(t.conv);
  const prose = lastAssistantProse(t.conv);
  ledger(`rt-2 fix turn ${t.seconds}s errors=${t.errors} proposal_status=${t.status} revision=${latest?.revision} model=${latest?.content?.model_id} kbs=${JSON.stringify(latest?.content?.knowledge_bases)} mentions-model=${/model/i.test(prose)} mentions-kb=${/knowledge/i.test(prose)}: ${prose.slice(0, 300).replace(/\n/g, " ")}`);
  await evidence(page, "rt2-fixed-in-one-revision");
  await attach(testInfo, "conversation-rt2.json", t.conv);
  stateSet("arch_rt2", { errors, fixStatus: t.status, fixSeconds: t.seconds, model: latest?.content?.model_id, kbs: latest?.content?.knowledge_bases });
  expect(t.status, "both errors fixed in one revision").toBe("draft");
  expect(ALLOWED).toContain(String(latest?.content?.model_id));
  expect((latest?.content?.knowledge_bases as string[]) ?? []).not.toContain(fakeKb);
  await rejectIfOffered(page, "rt-2");
  ledger("rt-2 end PASS");
});

test("rt-3: server model catalog — only GLM 5.3 / Kimi K3 proposable; SiliconFlow refused naming the SSM parameter; 'Kimi' → global.moonshotai.kimi-k3", async ({ page }, testInfo) => {
  ledger("rt-3 start");
  await setLanguage(page, "en");
  const caps = await (await page.request.get("/api/capabilities")).json();
  const models = (caps.models ?? []) as Array<{ model_id: string; provider_kind: string; available: boolean; default?: boolean }>;
  const available = models.filter((m) => m.available).map((m) => m.model_id);
  const sf = models.filter((m) => m.model_id.startsWith("siliconflow."));
  ledger(`rt-3 capabilities models=${JSON.stringify(models.map((m) => `${m.model_id}:${m.available ? "ok" : "unconfigured"}`))}`);
  await attach(testInfo, "capabilities-models.json", models);
  expect(available.sort()).toEqual([...ALLOWED].sort());
  expect(sf.length).toBe(4);
  expect(sf.every((m) => m.available === false), "every siliconflow row is not configured").toBe(true);
  const editorOpts = stateGet<Array<{ value: string; label: string; disabled: boolean }>>("arch_editor_models") ?? [];
  if (editorOpts.length) {
    expect(editorOpts.filter((o) => !o.disabled).map((o) => o.value).sort()).toEqual([...ALLOWED].sort());
    expect(editorOpts.filter((o) => o.disabled).every((o) => o.value.startsWith("siliconflow.") && /not configured/i.test(o.label))).toBe(true);
  }

  const id = await newConversation(page);
  const name = `${E2E_PREFIX}kimi-${stamp()}`;
  // (a) a SiliconFlow model → refusal that names the SSM parameter, never a draft with it
  const a = await sendTurn(page, id, `Build a tiny FAQ agent named ${name} that answers questions about Octank's office hours (9:00-18:00 CST, Mon-Fri) in one sentence. No tools, no knowledge base, no memory, English. Hard customer requirement: run it on DeepSeek-V4-Pro through SiliconFlow (model id siliconflow.deepseek-ai/DeepSeek-V4-Pro). Everything is final; write the proposal block now.`);
  const proseA = lastAssistantProse(a.conv);
  const latestA = latestProposal(a.conv);
  const namesSsm = proseA.includes(SILICONFLOW_SSM) || a.reply.includes(SILICONFLOW_SSM);
  const listsCatalog = ALLOWED.every((m) => proseA.includes(m) || a.reply.includes(m));
  ledger(`rt-3a ${a.seconds}s errors=${a.errors} proposal_status=${a.status} latest=${JSON.stringify({ status: latestA?.status, model: latestA?.content?.model_id, errors: latestA?.validation_errors }).slice(0, 400)} names-ssm=${namesSsm} lists-catalog=${listsCatalog}: ${proseA.slice(0, 500).replace(/\n/g, " ")}`);
  await evidence(page, "rt3-siliconflow-refused");
  await attach(testInfo, "conversation-rt3a.json", a.conv);
  expect(a.errors).toBe(0);
  if (latestA) {
    expect(latestA.status, "no approvable revision with a siliconflow model").not.toBe("draft");
    if (String(latestA.content?.model_id).startsWith("siliconflow.")) {
      expect((latestA.validation_errors ?? []).join(" "), "the server names the SSM parameter").toContain(SILICONFLOW_SSM);
    }
  }
  // soft: run B's refusal was right (catalog listed, "not configured", three options) but could not
  // name the parameter — the rendered catalog section carries no SSM name (N-ARCH-bug3); let (b) run
  expect.soft(namesSsm, `the refusal names the SSM parameter ${SILICONFLOW_SSM}`).toBe(true);
  expect(listsCatalog, "the refusal states the selectable catalog").toBe(true);

  // (b) "Kimi" → global.moonshotai.kimi-k3 (fix loop allowed, ≤ 2 more turns)
  const statuses: string[] = [];
  let conv = a.conv;
  let turns = 1;
  for (let i = 0; i < 2; i++) {
    turns += 1;
    const p = statuses[statuses.length - 1] === "invalid"
      ? "Please fix the validation errors with a patch block; keep Kimi and every confirmed decision."
      : "Understood. Then use Kimi instead — that is final, together with everything else above. Write the proposal block now.";
    const t = await sendTurn(page, id, p);
    conv = t.conv;
    statuses.push(t.status);
    const latest = latestProposal(conv);
    ledger(`rt-3b turn ${turns} ${t.seconds}s errors=${t.errors} proposal_status=${t.status} model=${latest?.content?.model_id} errors=${JSON.stringify(latest?.validation_errors ?? []).slice(0, 300)}: ${lastAssistantProse(conv).slice(0, 300).replace(/\n/g, " ")}`);
    await evidence(page, `rt3-kimi-turn-${turns}`);
    if (t.status === "draft") break;
  }
  const latestB = latestProposal(conv);
  await attach(testInfo, "conversation-rt3.json", conv);
  stateSet("arch_rt3", { namesSsm, listsCatalog, statuses, model: latestB?.content?.model_id });
  expect(statuses[statuses.length - 1], `a VALID Kimi proposal (statuses ${statuses.join("→")})`).toBe("draft");
  expect(latestB?.content?.model_id, "'Kimi' resolves to the catalog id").toBe("global.moonshotai.kimi-k3");
  const cardText = await page.getByTestId("v2-assistant-proposal").innerText();
  expect(cardText).toContain("global.moonshotai.kimi-k3");
  await evidence(page, "rt3-kimi-proposal-card");
  await rejectIfOffered(page, "rt-3");
  ledger("rt-3 end PASS");
});

test("rt-4: memory: workspace (CN self-hosted shared memory) validates, deploys ≤ 15 min, and a NEW chat session recalls the stored facts", async ({ page }, testInfo) => {
  ledger("rt-4 start");
  await setLanguage(page, "en");
  const status = await (await page.request.get("/api/assistant/architect")).json();
  ledger(`rt-4 capabilities=${JSON.stringify(status.capabilities)}`);
  expect(status.capabilities?.shared_memory, "the preset recognises the CN memory substrate").toBe(true);
  expect(status.capabilities?.memory_kind).toBe("selfhosted");

  const id = await newConversation(page);
  const name = `${E2E_PREFIX}memory-${stamp()}`;
  stateSet("arch_rt4_name", name);
  const brief = `Design an employee concierge agent for the fictional company Octank, named ${name}. It helps employees with everyday workplace questions in English and must remember each employee's stated preferences and facts across conversations: enable long-term memory using the workspace's shared memory (memory: workspace). No gateway tools, no skills, no knowledge base, no code interpreter. Default GLM model. Escalate anything it cannot answer to hr@octank.example. About 800 employees, business hours. No legacy system, no historical data, no compliance constraints beyond "no PII in logs"; take your library defaults for everything else as assumptions. All decisions are final; please write the proposal block now.`;
  const statuses: string[] = [];
  let conv: Record<string, unknown> = {};
  let turns = 0;
  let turn1Asked = false;
  for (let i = 0; i < 3; i++) {
    turns += 1;
    const p = statuses[statuses.length - 1] === "invalid"
      ? "Please fix the validation errors with a patch block; keep every confirmed decision including memory: workspace."
      : i === 0 ? brief : "Everything above is final, including memory: workspace. Output the proposal block now.";
    const t = await sendTurn(page, id, p);
    conv = t.conv;
    statuses.push(t.status);
    const prose = lastAssistantProse(conv);
    if (i === 0) turn1Asked = ((conv.proposals ?? []) as unknown[]).length === 0 && QUESTION_RE.test(prose);
    const latest = latestProposal(conv);
    ledger(`rt-4 turn ${turns} ${t.seconds}s errors=${t.errors} proposal_status=${t.status} memory=${latest?.content?.memory} errors=${JSON.stringify(latest?.validation_errors ?? []).slice(0, 300)}: ${prose.slice(0, 300).replace(/\n/g, " ")}`);
    await evidence(page, `rt4-turn-${turns}`);
    if (t.status === "draft") break;
  }
  const latest = latestProposal(conv);
  await attach(testInfo, "conversation-rt4.json", conv);
  ledger(`rt-4 statuses=${statuses.join("→")} turn1-asked=${turn1Asked} memory=${latest?.content?.memory} model=${latest?.content?.model_id}`);
  expect(statuses[statuses.length - 1], `a VALID proposal ≤ 3 turns (statuses ${statuses.join("→")})`).toBe("draft");
  expect(latest?.content?.memory, "memory: workspace accepted by validation").toBe("workspace");
  expect(turn1Asked, "a fully specified brief is not re-asked").toBe(false);
  const card = page.getByTestId("v2-assistant-proposal");
  await expect(card).toContainText(/workspace/);
  await evidence(page, "rt4-proposal-memory-workspace");

  // approve → deploy
  const approve = page.getByTestId("v2-assistant-proposal-approve");
  await expect(approve).toBeEnabled({ timeout: 60_000 });
  const t1 = Date.now();
  const approveRes = page.waitForResponse((r) => r.url().includes("/proposal/approve") && r.request().method() === "POST");
  await approve.click();
  const ok = page.getByTestId("v2-confirm-ok");
  if (await ok.isVisible({ timeout: 3000 }).catch(() => false)) await ok.click();
  const ar = await approveRes;
  const approved = await ar.json();
  ledger(`rt-4 approve → ${ar.status()} job=${approved.job_id} agent=${approved.agent?.id} started=${approved.started}`);
  expect([200, 202]).toContain(ar.status());
  const agentId = String(approved.agent?.id);
  stateSet("arch_rt4_agent_id", agentId);
  let lastKey = "";
  const job = await waitForJob(page.request, approved.job_id, {
    timeoutMs: 25 * 60 * 1000,
    onTick: (j) => {
      const ev = (j.events as Array<{ stage: string; msg: string }>) ?? [];
      const tail = ev[ev.length - 1];
      const key = tail ? `${tail.stage}:${tail.msg}` : "";
      if (key !== lastKey) {
        lastKey = key;
        ledger(`rt-4 job ${String(approved.job_id).slice(0, 8)} ${j.status} ${key.slice(0, 140)}`);
      }
    },
  });
  const deploySeconds = Math.round((Date.now() - t1) / 1000);
  await attach(testInfo, "deploy-job-rt4.json", job);
  await page.reload();
  await expect(page.getByTestId("v2-assistant-outcome")).toBeVisible({ timeout: 60_000 });
  await evidence(page, "rt4-deployed-outcome");
  ledger(`rt-4 deploy ${job.status} in ${deploySeconds}s`);
  expect(job.status, `deploy failed: ${job.error ?? ""}`).toBe("succeeded");
  expect(deploySeconds, "active ≤ 15 min").toBeLessThanOrEqual(15 * 60);
  const agent = await getAgent(page.request, agentId);
  const spec = (agent.spec ?? {}) as Record<string, unknown>;
  const mem = (spec.memory ?? {}) as { short_term?: boolean; long_term?: boolean; memory_id?: string };
  ledger(`rt-4 agent status=${agent.status} memory=${JSON.stringify(spec.memory)} model=${spec.model_id}`);
  await attach(testInfo, "agent-rt4.json", agent);
  expect(agent.status).toBe("active");
  expect(!!mem.long_term, "long_term memory on the deployed spec").toBe(true);

  // chat 1: store facts
  const facts = { manager: "Priya Raman", costCenter: "CC-4471" };
  const c1 = await chatOnce(page, agentId, `Please remember two things about me: my manager is ${facts.manager} and my cost center is ${facts.costCenter}. Just confirm you noted them.`);
  ledger(`rt-4 chat1 ${c1.seconds}s errors=${c1.errors} session=${c1.sessionId}: ${c1.answer.slice(0, 200).replace(/\n/g, " ")}`);
  await attach(testInfo, "chat-rt4-facts.txt", c1.answer);
  await evidence(page, "rt4-chat-1-facts");
  expect(c1.errors).toBe(0);
  expect(looksLikeError(c1.answer)).toBe(false);

  // long-term records for this actor (≤ 10 min), then a NEW session recalls them
  const tWait = Date.now();
  let summary = (await (await page.request.get(`/api/chat/${agentId}/memory?session_id=${encodeURIComponent(c1.sessionId)}`)).json()) as { actor_id?: string; records?: unknown[]; events?: unknown[] };
  let records = summary.records ?? [];
  while (records.length < 1 && Date.now() - tWait < 10 * 60 * 1000) {
    await sleep(10_000);
    summary = (await (await page.request.get(`/api/chat/${agentId}/memory?session_id=${encodeURIComponent(c1.sessionId)}`)).json()) as typeof summary;
    records = summary.records ?? [];
  }
  const extractSeconds = Math.round((Date.now() - tWait) / 1000);
  ledger(`rt-4 long-term records=${records.length} after ${extractSeconds}s actor=${summary.actor_id}`);
  await attach(testInfo, "long-term-records-rt4.json", records);
  if (summary.actor_id) {
    await page.goto(`/v2/memory?tab=long-term&actor=${encodeURIComponent(summary.actor_id)}`);
    await expect(page.getByTestId("v2-memory-longterm")).toBeVisible({ timeout: 60_000 }).catch(() => {});
    await evidence(page, "rt4-memory-long-term");
  }
  const c2 = await chatOnce(page, agentId, "Who is my manager, and what is my cost center? Answer with both.");
  const hits = [new RegExp(facts.manager, "i").test(c2.answer), new RegExp(facts.costCenter.replace("-", "[- ]?"), "i").test(c2.answer)].filter(Boolean).length;
  ledger(`rt-4 chat2 (new session ${c2.sessionId}) ${c2.seconds}s errors=${c2.errors} recall=${hits}/2: ${c2.answer.slice(0, 300).replace(/\n/g, " ")}`);
  await attach(testInfo, "chat-rt4-recall.txt", c2.answer);
  await evidence(page, "rt4-chat-2-recall-new-session");
  stateSet("arch_rt4", { deploySeconds, memory: spec.memory, records: records.length, extractSeconds, recall: hits, session1: c1.sessionId, session2: c2.sessionId });
  expect(c2.sessionId).not.toBe(c1.sessionId);
  expect(c2.errors).toBe(0);
  expect(records.length, "≥ 1 long-term record within 10 min").toBeGreaterThanOrEqual(1);
  expect(hits, "the new session recalls both facts").toBe(2);
  ledger(`rt-4 end PASS agent=${agentId} deploy=${deploySeconds}s recall=${hits}/2`);
});

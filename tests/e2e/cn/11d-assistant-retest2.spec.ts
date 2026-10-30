import { expect, test } from "@playwright/test";
import { ALLOWED, asksAboutMemory, asksMember, hasNumberedList, lastAssistantProse, latestProposal, newConversation, recordFirstProposal, rejectIfOffered, sendTurn, SILICONFLOW_SSM, stamp, turnFailures } from "./assistant-helpers";
import { attach, E2E_PREFIX, evidence, ledger, setLanguage, stateSet } from "./helpers";

/**
 * arch-retest2 (2026-10-14) — the architect preset release 1.6.1-d0bcc666d0e7 (r7-F: ask-first for open
 * material decisions, SSM parameter on unconfigured catalog rows, selectable-only model hint, trajectory
 * self-check) on the default environment, through the UI/API only. Confirms the re-test findings:
 *
 *  rt2-1a  N-ARCH-bug2: a brief that leaves long-term memory UNSPECIFIED (workspace has shared memory)
 *          gets a clarifying QUESTION in the FIRST reply — one numbered list whose memory item offers
 *          workspace / disabled — and NO proposal; answering "workspace" yields a valid proposal with
 *          `memory: workspace` at the next turn (fix loop allowed).
 *  rt2-1b  r7-F clause: a brief that states memory by unambiguous MEANING ("must remember … across
 *          conversations", no literal mode) is NOT asked about memory; the proposal carries
 *          `memory: workspace`.
 *  rt2-3   N-ARCH-bug3: a SiliconFlow model is refused AND the refusal names the SSM parameter
 *          `launchpad-siliconflow-api-key`; "Kimi" → `global.moonshotai.kimi-k3`.
 *  rt2-obs9 a PUT with a foreign model id: the server's "choose one of" lists ONLY the two selectable
 *          ids (no `siliconflow.*`); approve of that revision is refused.
 *
 * Every case records its first proposal for the N-ARCH-obs10 ratio (`arch_first_proposals` in the
 * shared state). Checks are collected while the flow continues and asserted at the end of each case,
 * so a preset miss in the first half still leaves the second half recorded. Test data is `e2ea-…`
 * (N10_PREFIX=e2ea-); conversation ids go to the shared state for 11z-assistant-cleanup.spec.ts.
 */

test.describe.configure({ mode: "default" });

const WORKSPACE_RE = /\bworkspace\b/i;
const DISABLED_RE = /\bdisabled\b|\bno memory\b|\bwithout memory\b/i;
const FORBIDDEN_DEFAULT_RE = /flagged as (an )?assumption|safe default/i;

/** Up to `max` further turns: answer `first`, then fix / push until a draft (or the turns run out). */
async function driveToDraft(page: import("@playwright/test").Page, id: string, label: string, first: string, max: number, startTurn: number): Promise<{ statuses: string[]; conv: Record<string, unknown>; turns: number; memoryReAsked: boolean }> {
  const statuses: string[] = [];
  let conv: Record<string, unknown> = {};
  let turns = startTurn;
  let memoryReAsked = false;
  for (let i = 0; i < max; i++) {
    turns += 1;
    const p = statuses[statuses.length - 1] === "invalid"
      ? "Please fix the validation errors with a patch block; keep every confirmed decision."
      : i === 0 ? first : "Everything above is final. Output the proposal block now.";
    const t = await sendTurn(page, id, p);
    conv = t.conv;
    statuses.push(t.status);
    const prose = lastAssistantProse(conv);
    const memQ = asksAboutMemory(prose);
    if (i === 0) memoryReAsked = memQ;
    const latest = latestProposal(conv);
    ledger(`${label} turn ${turns} ${t.seconds}s errors=${t.errors} proposal_status=${t.status} memory=${latest?.content?.memory} memory-question=${memQ} errors=${JSON.stringify(latest?.validation_errors ?? []).slice(0, 300)}: ${prose.slice(0, 400).replace(/\n/g, " ")}`);
    await evidence(page, `${label}-turn-${turns}`);
    if (t.status === "draft") break;
  }
  return { statuses, conv, turns, memoryReAsked };
}

async function activeKbName(page: import("@playwright/test").Page): Promise<string | undefined> {
  const kbs = ((await (await page.request.get("/api/knowledge-bases")).json()).items ?? []) as Array<{ name: string; status: string }>;
  return kbs.find((k) => k.status === "ACTIVE")?.name;
}

test("rt2-1a: memory left unspecified → first reply is ONE numbered question list offering workspace/disabled, no proposal; 'workspace' → valid proposal next turn", async ({ page }, testInfo) => {
  ledger("rt2-1a start");
  await setLanguage(page, "en");
  const kb = await activeKbName(page);
  const id = await newConversation(page);
  const name = `${E2E_PREFIX}ask-${stamp()}`;
  // NB: nothing about memory, no "decide yourself", no "all decisions are final", no "write the proposal now"
  const brief = `Design an internal IT helpdesk agent for the fictional company Octank, named ${name}. It answers employees' IT questions (VPN, laptop, password reset, onboarding accounts) in English, concisely, and escalates anything it cannot resolve to it-support@octank.example. ${kb ? `Attach the existing knowledge base "${kb}" from the catalog for policy look-ups.` : "No knowledge base."} No gateway tools, no skills. Default GLM model. Target users: about 800 employees, business hours only.`;
  const t1 = await sendTurn(page, id, brief);
  const prose1 = lastAssistantProse(t1.conv);
  const proposals1 = ((t1.conv.proposals ?? []) as unknown[]).length;
  const turn1 = {
    seconds: t1.seconds,
    errors: t1.errors,
    status: t1.status,
    proposals: proposals1,
    asks: asksMember(prose1),
    memoryQuestion: asksAboutMemory(prose1),
    numberedList: hasNumberedList(prose1),
    offersWorkspace: WORKSPACE_RE.test(prose1),
    offersDisabled: DISABLED_RE.test(prose1),
    forbiddenDefault: FORBIDDEN_DEFAULT_RE.test(prose1) && proposals1 > 0,
    questionCount: (prose1.match(/\?|？/g) ?? []).length,
  };
  ledger(`rt2-1a turn 1 ${JSON.stringify(turn1)}: ${prose1.slice(0, 700).replace(/\n/g, " ")}`);
  await evidence(page, "rt2-1a-turn-1-question");
  await attach(testInfo, "conversation-rt2-1a-turn1.json", t1.conv);

  // answer the memory question (and keep everything else as stated) → proposal with memory: workspace
  const answer = `Workspace — enable long-term memory using the workspace's shared memory (memory: workspace). Everything else stays as stated: name ${name}, default GLM model (global.zai.glm-5.3), knowledge base ${kb ? `"${kb}"` : "none"}, no gateway tools, no skills, no code interpreter, English only, escalate to it-support@octank.example, about 800 employees, business hours; no legacy system, no historical data, no compliance constraints beyond "no PII in logs"; accept your library defaults for anything else and record them as assumptions. All decisions are final; please write the proposal block now.`;
  const d = await driveToDraft(page, id, "rt2-1a", answer, 2, 1);
  const latest = latestProposal(d.conv);
  const card = await page.getByTestId("v2-assistant-proposal-view").isVisible().catch(() => false);
  if (card) await evidence(page, "rt2-1a-proposal-workspace");
  const first = recordFirstProposal("rt2-1a", d.conv);
  ledger(`rt2-1a statuses=${d.statuses.join("→")} turns=${d.turns} card=${card} memory=${latest?.content?.memory} model=${latest?.content?.model_id} re-asked-memory=${d.memoryReAsked}`);
  await attach(testInfo, "conversation-rt2-1a.json", d.conv);
  stateSet("arch_rt2_1a", { turn1, statuses: d.statuses, turns: d.turns, memory: latest?.content?.memory, reAsked: d.memoryReAsked, first });
  await rejectIfOffered(page, "rt2-1a");

  // ── verdict (N-ARCH-bug2) ────────────────────────────────────────────────
  expect(turn1.errors, "turn 1 without error bubbles").toBe(0);
  expect(turn1.proposals, "NO proposal while the memory decision is open").toBe(0);
  expect(turn1.asks, "the first reply asks").toBe(true);
  expect(turn1.memoryQuestion, "the first reply asks about long-term memory").toBe(true);
  expect(turn1.numberedList, "the open decisions come as ONE numbered list").toBe(true);
  expect(turn1.offersWorkspace && turn1.offersDisabled, "the memory question offers workspace and disabled").toBe(true);
  expect(turn1.forbiddenDefault, "no 'safe default flagged as an assumption'").toBe(false);
  expect(d.memoryReAsked, "memory is not re-asked once answered").toBe(false);
  expect(d.statuses[d.statuses.length - 1], `a VALID proposal after the answer (statuses ${d.statuses.join("→")})`).toBe("draft");
  expect(latest?.content?.memory, "the answer 'workspace' is honoured").toBe("workspace");
  expect(ALLOWED).toContain(String(latest?.content?.model_id));
  ledger("rt2-1a end PASS");
});

test("rt2-1b: memory stated by unambiguous meaning ('remember … across conversations') → NOT asked about memory; proposal carries memory: workspace", async ({ page }, testInfo) => {
  ledger("rt2-1b start");
  await setLanguage(page, "en");
  const kb = await activeKbName(page);
  const id = await newConversation(page);
  const name = `${E2E_PREFIX}meaning-${stamp()}`;
  // NB: no literal mode word ("memory: workspace" / "disabled" / "long-term memory"), no delegation phrase
  const brief = `Design an employee concierge agent for the fictional company Octank, named ${name}. It helps employees with everyday workplace questions in English and must remember each employee's stated preferences and earlier questions across conversations, so a returning employee never has to repeat them. ${kb ? `Attach the existing knowledge base "${kb}" from the catalog for policy look-ups.` : "No knowledge base."} No gateway tools, no skills, no code interpreter. Default GLM model (global.zai.glm-5.3). Escalate anything it cannot answer to hr@octank.example. About 800 employees, business hours only. No legacy system, no historical data, no compliance constraints beyond "no PII in logs"; take your library defaults for everything else and record them as assumptions. Please write the proposal block.`;
  const t1 = await sendTurn(page, id, brief);
  const prose1 = lastAssistantProse(t1.conv);
  const proposals1 = ((t1.conv.proposals ?? []) as unknown[]).length;
  const turn1 = { seconds: t1.seconds, errors: t1.errors, failures: turnFailures(t1.conv, 1), status: t1.status, proposals: proposals1, asks: asksMember(prose1), memoryQuestion: asksAboutMemory(prose1), memory: latestProposal(t1.conv)?.content?.memory, validationErrors: latestProposal(t1.conv)?.validation_errors ?? [] };
  ledger(`rt2-1b turn 1 ${JSON.stringify(turn1)}: ${prose1.slice(0, 700).replace(/\n/g, " ")}`);
  await evidence(page, "rt2-1b-turn-1");
  await attach(testInfo, "conversation-rt2-1b-turn1.json", t1.conv);

  // proposal at turn 1 expected; a fix loop (invalid → patch) or an unrelated question gets ≤ 2 more turns
  let statuses = [t1.status];
  let conv = t1.conv;
  let turns = 1;
  if (t1.status !== "draft") {
    const followUp = proposals1 === 0
      ? "Keep the brief exactly as stated — every decision in it is final. Output the proposal block now."
      : "Please fix the validation errors with a patch block; keep every confirmed decision.";
    const d = await driveToDraft(page, id, "rt2-1b", followUp, 2, 1);
    statuses = [...statuses, ...d.statuses];
    conv = d.conv;
    turns = d.turns;
  }
  const latest = latestProposal(conv);
  const card = await page.getByTestId("v2-assistant-proposal-view").isVisible().catch(() => false);
  if (card) await evidence(page, "rt2-1b-proposal-card");
  const first = recordFirstProposal("rt2-1b", conv);
  ledger(`rt2-1b statuses=${statuses.join("→")} turns=${turns} card=${card} memory=${latest?.content?.memory} model=${latest?.content?.model_id}`);
  await attach(testInfo, "conversation-rt2-1b.json", conv);
  stateSet("arch_rt2_1b", { turn1, statuses, turns, memory: latest?.content?.memory, first });
  await rejectIfOffered(page, "rt2-1b");

  // ── verdict (r7-F "unambiguous meaning" clause) ─────────────────────────
  // an `invalid` first revision shows the designed proposal_rejected row (counted by the UI as an
  // error bubble) — only turn FAILURES count here
  expect(turn1.failures, "turn 1 did not fail").toEqual([]);
  expect(turn1.memoryQuestion, "memory stated by meaning is NOT asked").toBe(false);
  expect(turn1.proposals, "a proposal at turn 1 (the brief states every material decision)").toBeGreaterThan(0);
  expect(statuses[statuses.length - 1], `a VALID proposal (statuses ${statuses.join("→")})`).toBe("draft");
  expect(latest?.content?.memory, "'remember … across conversations' ⇒ memory: workspace").toBe("workspace");
  ledger("rt2-1b end PASS");
});

test("rt2-3: SiliconFlow model refused naming the SSM parameter; 'Kimi' → global.moonshotai.kimi-k3", async ({ page }, testInfo) => {
  ledger("rt2-3 start");
  await setLanguage(page, "en");
  const caps = await (await page.request.get("/api/capabilities")).json();
  const models = (caps.models ?? []) as Array<{ model_id: string; available: boolean }>;
  const available = models.filter((m) => m.available).map((m) => m.model_id);
  const sf = models.filter((m) => m.model_id.startsWith("siliconflow."));
  ledger(`rt2-3 capabilities models=${JSON.stringify(models.map((m) => `${m.model_id}:${m.available ? "ok" : "unconfigured"}`))}`);
  expect(available.sort()).toEqual([...ALLOWED].sort());
  expect(sf.length).toBe(4);
  expect(sf.every((m) => m.available === false)).toBe(true);

  const id = await newConversation(page);
  const name = `${E2E_PREFIX}kimi-${stamp()}`;
  // (a) a SiliconFlow model → refusal naming the SSM parameter, never a draft with it
  const a = await sendTurn(page, id, `Build a tiny FAQ agent named ${name} that answers questions about Octank's office hours (9:00-18:00 CST, Mon-Fri) in one sentence. No tools, no knowledge base, no memory, English. Hard customer requirement: run it on DeepSeek-V4-Pro through SiliconFlow (model id siliconflow.deepseek-ai/DeepSeek-V4-Pro). Everything is final; write the proposal block now.`);
  const proseA = lastAssistantProse(a.conv);
  const latestA = latestProposal(a.conv);
  const refusal = {
    seconds: a.seconds,
    errors: a.errors,
    status: a.status,
    proposals: ((a.conv.proposals ?? []) as unknown[]).length,
    latestStatus: latestA?.status,
    latestModel: latestA?.content?.model_id,
    namesSsm: proseA.includes(SILICONFLOW_SSM) || a.reply.includes(SILICONFLOW_SSM),
    listsCatalog: ALLOWED.every((m) => proseA.includes(m) || a.reply.includes(m)),
    mentionsSecureString: /SecureString/i.test(proseA),
    mentionsBootstrap: /bootstrap/i.test(proseA),
    claimsConsoleSetting: /model settings|workspace settings/i.test(proseA),
  };
  ledger(`rt2-3a ${JSON.stringify(refusal)}: ${proseA.slice(0, 700).replace(/\n/g, " ")}`);
  await evidence(page, "rt2-3-siliconflow-refused-ssm");
  await attach(testInfo, "conversation-rt2-3a.json", a.conv);

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
    ledger(`rt2-3b turn ${turns} ${t.seconds}s errors=${t.errors} proposal_status=${t.status} model=${latest?.content?.model_id} errors=${JSON.stringify(latest?.validation_errors ?? []).slice(0, 300)}: ${lastAssistantProse(conv).slice(0, 300).replace(/\n/g, " ")}`);
    await evidence(page, `rt2-3-kimi-turn-${turns}`);
    if (t.status === "draft") break;
  }
  const latestB = latestProposal(conv);
  const cardText = (await page.getByTestId("v2-assistant-proposal").innerText().catch(() => "")) ?? "";
  if (cardText) await evidence(page, "rt2-3-kimi-proposal-card");
  const first = recordFirstProposal("rt2-3", conv);
  await attach(testInfo, "conversation-rt2-3.json", conv);
  stateSet("arch_rt2_3", { refusal, statuses, model: latestB?.content?.model_id, first });
  await rejectIfOffered(page, "rt2-3");

  // ── verdict (N-ARCH-bug3) ────────────────────────────────────────────────
  expect(refusal.errors).toBe(0);
  if (latestA) {
    expect(latestA.status, "no approvable revision with a siliconflow model").not.toBe("draft");
  }
  expect(refusal.namesSsm, `the refusal names the SSM parameter ${SILICONFLOW_SSM}`).toBe(true);
  expect(refusal.listsCatalog, "the refusal states the selectable catalog").toBe(true);
  expect(statuses[statuses.length - 1], `a VALID Kimi proposal (statuses ${statuses.join("→")})`).toBe("draft");
  expect(latestB?.content?.model_id, "'Kimi' resolves to the catalog id").toBe("global.moonshotai.kimi-k3");
  expect(cardText).toContain("global.moonshotai.kimi-k3");
  ledger("rt2-3 end PASS");
});

test("rt2-obs9: a PUT with a foreign model id → 'choose one of' lists ONLY the two selectable ids; approve refused", async ({ page }, testInfo) => {
  ledger("rt2-obs9 start");
  await setLanguage(page, "en");
  const id = await newConversation(page);
  const name = `${E2E_PREFIX}foreign-${stamp()}`;
  const edited = {
    version: 1, name, model_id: "anthropic.claude-sonnet-4-5", model_source: "bedrock",
    system_prompt: "Answer questions about Octank's office hours (9:00-18:00 CST, Monday to Friday) in one sentence.",
    tools: [], skills: [], knowledge_bases: [], memory: "disabled",
  };
  const put = await page.request.put(`/api/assistant/architect/conversations/${id}/proposal`, { data: { content: edited } });
  const body = await put.json().catch(() => ({}));
  const errors = ((body.proposal?.validation_errors ?? []) as string[]);
  const modelErr = errors.find((e) => /model_id/i.test(e)) ?? "";
  const choose = modelErr.split(/choose one of:?/i)[1] ?? "";
  const offered = [...choose.matchAll(/[A-Za-z0-9_.\-/]+\.[A-Za-z0-9_.\-/]+/g)].map((m) => m[0]).filter((s) => s.includes("."));
  ledger(`rt2-obs9 PUT → ${put.status()} status=${body.proposal?.status ?? body.code} errors(${errors.length})=${JSON.stringify(errors).slice(0, 600)} offered=${JSON.stringify(offered)}`);
  await attach(testInfo, "put-foreign-model.json", { status: put.status(), body, offered });
  await page.reload();
  await expect(page.getByTestId("v2-assistant-proposal-view")).toBeVisible({ timeout: 60_000 });
  await evidence(page, "rt2-obs9-invalid-card-selectable-only");
  let approveStatus: number | undefined;
  let approveCode = "";
  if (put.status() === 200 && body.proposal) {
    const ap = await page.request.post(`/api/assistant/architect/conversations/${id}/proposal/approve`, { data: { revision: body.proposal.revision, content_hash: body.proposal.content_hash } });
    approveStatus = ap.status();
    approveCode = String(((await ap.json().catch(() => ({}))) as { code?: string }).code ?? "");
    ledger(`rt2-obs9 approve invalid revision → ${approveStatus} ${approveCode}`);
  }
  stateSet("arch_rt2_obs9", { put: put.status(), status: body.proposal?.status, errors, offered, approveStatus, approveCode });

  // ── verdict (N-ARCH-obs9) ────────────────────────────────────────────────
  expect(put.status()).toBe(200);
  expect(body.proposal?.status).toBe("invalid");
  expect(modelErr, "a model_id line").toMatch(/not available in this build/);
  expect(offered.sort(), "'choose one of' lists exactly the selectable ids").toEqual([...ALLOWED].sort());
  expect(errors.join(" "), "no unconfigured siliconflow id offered").not.toMatch(/siliconflow\./);
  expect(approveStatus, "approve of the invalid revision is refused").toBeGreaterThanOrEqual(400);
  ledger("rt2-obs9 end PASS");
});

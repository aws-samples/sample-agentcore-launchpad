import { expect, test } from "@playwright/test";
import { attach, chatOnce, E2E_PREFIX, evidence, getAgent, ledger, listAgents, looksLikeError, setLanguage, sleep, stateGet, stateSet, waitForJob } from "./helpers";

const PRESET = "aws-agent-solution-architect";
const PRESET_LABEL = "AWS Agent Solution Architect";

/**
 * arch-e2e (2026-10-12) — the architect assistant as a customer uses it, against an
 * ALREADY INSTALLED preset (never installs / repairs / uninstalls it):
 *
 *  arch-1  V2 nav entry (en + zh-CN) → /v2/assistant without the "not running" notice;
 *          GET /api/system-agents shows the preset active with version/release/last_job;
 *          Agents → create → "Design with the architect assistant" also lands there.
 *  arch-2  a realistic brief (internal IT helpdesk: KB + hr-database gateway tool +
 *          long-term memory) → VALID proposal ≤ 3 turns; records the fence form the
 *          model used (plain vs ```json launchpad-proposal — both accepted since N10v3-bug4)
 *          and the proposal's choices (name / model / tools / KB / memory).
 *  arch-3  approve → deploy job → active ≤ 15 min → Chat answers with GLM text; the agent
 *          is a normal row (owner admin, no system_key, delete offered) whose spec reflects
 *          the proposal.
 *  arch-5  the preset's own invocations and the new agent's chat session show up in
 *          Observability (traces + sessions; system traces behind the "show system" box).
 *
 * Test data is `e2ea-…` (N10_PREFIX=e2ea-); ids are kept in the shared state file for
 * 11z-assistant-cleanup.spec.ts.
 */

test.describe.configure({ mode: "serial" });

function rememberConversation(id: string | null): void {
  if (!id) return;
  const cur = stateGet<string[]>("arch_conversations") ?? [];
  if (!cur.includes(id)) stateSet("arch_conversations", [...cur, id]);
}

test("arch-1: nav entry (en/zh-CN) → /v2/assistant available; preset active in /api/system-agents; wizard entry", async ({ page }, testInfo) => {
  ledger("arch-1 start");
  const sys = await (await page.request.get("/api/system-agents")).json();
  const preset = ((sys.presets ?? []) as Array<Record<string, unknown>>).find((p) => p.key === PRESET);
  await attach(testInfo, "system-agents.json", sys);
  ledger(`arch-1 preset=${JSON.stringify({ status: preset?.status, version: preset?.version, release: preset?.release, installed_release: preset?.installed_release, last_job: preset?.last_job })}`);
  expect(preset?.status, "preset active").toBe("active");
  for (const k of ["version", "release", "installed_release", "last_job"]) expect(Object.keys(preset ?? {}), `preset carries ${k}`).toContain(k);
  expect((preset?.last_job as { status?: string } | undefined)?.status).toBe("succeeded");

  const status = await (await page.request.get("/api/assistant/architect")).json();
  await attach(testInfo, "assistant-status.json", status);
  ledger(`arch-1 assistant available=${status.available} reasons=${JSON.stringify(status.reasons)} can_deploy=${status.can_deploy} capabilities=${JSON.stringify(status.capabilities)}`);
  expect(status.available, `assistant available: ${JSON.stringify(status.reasons)}`).toBe(true);

  // ── V2 nav (en) ─────────────────────────────────────────────────────────
  await setLanguage(page, "en");
  await page.goto("/v2");
  const nav = page.getByTestId("v2-nav-/v2/assistant");
  await expect(nav).toBeVisible({ timeout: 60_000 });
  const enLabel = (await nav.innerText()).trim();
  await evidence(page, "arch1-nav-en");
  await nav.click();
  await expect(page).toHaveURL(/\/v2\/assistant/);
  await expect(page.getByTestId("v2-assistant-new")).toBeVisible({ timeout: 60_000 });
  const unavailable = await page.getByTestId("v2-assistant-unavailable").count();
  await evidence(page, "arch1-assistant-page-en");
  ledger(`arch-1 nav en label="${enLabel}" url=${page.url()} unavailable-notice=${unavailable}`);
  expect(enLabel).toMatch(/Architect assistant/i);
  expect(unavailable, "no 'preset not running' notice").toBe(0);

  // ── V2 nav (zh-CN) ──────────────────────────────────────────────────────
  await setLanguage(page, "zh-CN");
  const zhLabel = (await page.getByTestId("v2-nav-/v2/assistant").innerText()).trim();
  await expect(page.getByTestId("v2-assistant-new")).toBeVisible({ timeout: 60_000 });
  const unavailableZh = await page.getByTestId("v2-assistant-unavailable").count();
  await evidence(page, "arch1-assistant-page-zh");
  ledger(`arch-1 nav zh label="${zhLabel}" unavailable-notice=${unavailableZh}`);
  expect(zhLabel).toContain("架构助手");
  expect(unavailableZh).toBe(0);
  await setLanguage(page, "en");

  // ── classic entry: Agents → create → "Design with the architect assistant" ─
  await page.goto("/v2/agents");
  await page.getByTestId("v2-agent-new").click();
  const link = page.getByRole("link", { name: /Design with the architect assistant/i }); // the wizard's own entry, not the sidebar link
  await expect(link).toBeVisible({ timeout: 30_000 });
  const linkText = (await link.innerText()).trim();
  await evidence(page, "arch1-wizard-assistant-entry");
  await link.click();
  await expect(page).toHaveURL(/\/v2\/assistant/);
  await expect(page.getByTestId("v2-assistant-new")).toBeVisible({ timeout: 60_000 });
  ledger(`arch-1 wizard link="${linkText}" → ${page.url()}`);
  // the legacy /create/assistant route is redirected to the V2 page
  await page.goto("/create/assistant");
  await expect(page.getByTestId("v2-assistant-new")).toBeVisible({ timeout: 60_000 });
  ledger(`arch-1 /create/assistant → ${page.url()}`);
  ledger("arch-1 end PASS");
});

test("arch-2/3: helpdesk brief → valid proposal ≤3 turns → approve → active ≤15 min → Chat; spec reflects the proposal", async ({ page }, testInfo) => {
  // ARCH_REUSE=1: the proposal/deploy half already ran (ids in the shared state) — re-run only the
  // post-deploy checks (agent affordances, Chat) against that agent
  const reuseId = process.env.ARCH_REUSE ? stateGet<string>("arch_agent_id") : undefined;
  const reuseChoices = (stateGet<{ choices?: Record<string, unknown> }>("arch_proposal") ?? {}).choices ?? {};
  ledger(reuseId ? `arch-2/3 REUSE agent=${reuseId}` : "arch-2 start");
  if (!reuseId) {
  const kbs = ((await (await page.request.get("/api/knowledge-bases")).json()).items ?? []) as Array<{ name: string; status: string }>;
  const kb = kbs.find((k) => k.status === "ACTIVE");
  const records = ((await (await page.request.get("/api/registry/records")).json()).records ?? []) as Array<{ name: string; type: string; status: string }>;
  const hrdb = records.find((r) => r.name === "hr-database" && r.status === "APPROVED");
  ledger(`arch-2 catalog: kb=${kb?.name ?? "none"} hr-database=${hrdb ? "APPROVED" : "missing"}`);

  await setLanguage(page, "en");
  await page.goto("/v2/assistant");
  await page.getByTestId("v2-assistant-new").click();
  await expect(page.getByTestId("v2-assistant-detail")).toBeVisible({ timeout: 60_000 });
  const conversationId = await page.getByTestId("v2-assistant-detail").getAttribute("data-conversation");
  rememberConversation(conversationId);
  ledger(`arch-2 conversation=${conversationId}`);
  const stamp = new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "");
  const newName = `${E2E_PREFIX}helpdesk-${stamp}`;
  stateSet("arch_agent_name", newName);
  const kbClause = kb ? `Attach the existing knowledge base "${kb.name}" from the catalog for policy look-ups.` : "No knowledge base.";
  const toolClause = hrdb ? `Attach the "hr-database" gateway tool so it can look up an employee's department and manager.` : "No gateway tools.";
  const prompts = [
    `Design an internal IT helpdesk agent for the fictional company Octank, named ${newName}. It answers employees' IT questions (VPN, laptop, password reset, onboarding accounts) in English, concisely, and escalates anything it cannot resolve to it-support@octank.example. ${kbClause} ${toolClause} Use long-term memory if this workspace offers it, otherwise run without memory — decide yourself, do not ask. Default GLM model. All decisions are final; please write the proposal now.`,
    `Yes — all of that is final: name ${newName}, GLM default model, the knowledge base and hr-database tool as discussed, memory as you decided. Produce the proposal block now.`,
    `Proceed with your best assumptions and output the proposal block.`,
  ];
  const fixPrompt = "Please fix the validation errors with a patch block; keep every confirmed decision and drop anything the workspace cannot provide (e.g. memory, evaluators that need data I did not give you).";
  let turns = 0;
  const proposal = page.getByTestId("v2-assistant-proposal-view");
  const tConv = Date.now();
  let conv: Record<string, unknown> = {};
  const statuses: string[] = [];
  const turnSeconds: number[] = [];
  for (let i = 0; i < prompts.length; i++) {
    turns += 1;
    const last = statuses[statuses.length - 1];
    const p = last === "invalid" ? fixPrompt : prompts[i];
    const t0 = Date.now();
    await page.getByTestId("v2-assistant-input").fill(p);
    await page.getByTestId("v2-assistant-send").click();
    await expect(page.getByTestId("v2-assistant-streaming")).toHaveCount(0, { timeout: 8 * 60 * 1000 });
    await expect(page.getByTestId("v2-assistant-input")).toBeEnabled({ timeout: 60_000 });
    turnSeconds.push(Math.round((Date.now() - t0) / 1000));
    const errors = await page.getByTestId("v2-assistant-turn-error").count();
    const thread = (await page.getByTestId("v2-assistant-thread").innerText()).slice(-500).replace(/\n/g, " ");
    conv = await (await page.request.get(`/api/assistant/architect/conversations/${conversationId}`)).json();
    const st = String(conv.proposal_status ?? "none");
    statuses.push(st);
    const verr = ((conv.proposals ?? []) as Array<{ validation_errors?: unknown }>).map((x) => x.validation_errors).filter((v) => Array.isArray(v) && v.length);
    ledger(`arch-2 turn ${turns} ${turnSeconds[turns - 1]}s errors=${errors} proposal_status=${st} validation_errors=${JSON.stringify(verr).slice(0, 400)}: …${thread}`);
    await evidence(page, `arch2-turn-${turns}`);
    if ((await proposal.isVisible().catch(() => false)) && st !== "invalid") break;
  }
  await attach(testInfo, "conversation.json", conv);
  const texts = ((conv.messages ?? []) as Array<{ role?: string; text?: string }>).map((m) => String(m.text ?? ""));
  const fences = [...new Set(texts.flatMap((t) => [...t.matchAll(/(```+|~~~+)([^\n]*launchpad-proposal[^\n]*)\n/g)].map((m) => m[2].trim())))];
  const jsonFenced = fences.some((f) => /^json\b/i.test(f));
  const proposals = (conv.proposals ?? []) as Array<{ revision: number; status: string; content: Record<string, unknown>; validation_errors?: unknown[] }>;
  const current = proposals.filter((p) => p.status !== "superseded").sort((a, b) => b.revision - a.revision)[0] ?? proposals[proposals.length - 1];
  const pc = (current?.content ?? {}) as Record<string, unknown>;
  const choices = { name: pc.name, model_id: pc.model_id, tools: pc.tools, tool_functions: pc.tool_functions, knowledge_bases: pc.knowledge_bases, memory: pc.memory, skills: pc.skills, native_tools: pc.native_tools, builtin_tools: pc.builtin_tools, revision: current?.revision, status: current?.status };
  ledger(`arch-2 fences=${JSON.stringify(fences)} json-prefixed=${jsonFenced} turns=${turns} statuses=${statuses.join("→")} choices=${JSON.stringify(choices)}`);
  await attach(testInfo, "proposal-choices.json", { fences, jsonFenced, turns, turnSeconds, statuses, choices, seconds: Math.round((Date.now() - tConv) / 1000) });
  stateSet("arch_proposal", { fences, jsonFenced, turns, turnSeconds, statuses, choices });
  expect(await proposal.isVisible(), `a proposal must appear within 3 turns (took ${turns}; fences ${JSON.stringify(fences)}; statuses ${statuses.join("→")})`).toBe(true);
  expect(statuses[statuses.length - 1], `the proposal must be VALID (approvable) within 3 turns (statuses ${statuses.join("→")})`).not.toBe("invalid");
  expect(turns).toBeLessThanOrEqual(3);
  const proposedName = (await page.getByTestId("v2-assistant-proposal-name").innerText()).trim();
  const approve = page.getByTestId("v2-assistant-proposal-approve");
  await expect(approve, "the proposal card offers Approve").toBeVisible();
  ledger(`arch-2 proposal after ${turns} turn(s) in ${Math.round((Date.now() - tConv) / 1000)}s name=${proposedName} revision=${await proposal.getAttribute("data-revision")}`);
  await evidence(page, "arch2-proposal-card");
  expect(proposedName).toBe(newName);
  ledger("arch-2 end PASS");

  // ── arch-3: approve → deploy ─────────────────────────────────────────────
  ledger("arch-3 start");
  await expect(approve).toBeEnabled({ timeout: 60_000 });
  const t1 = Date.now();
  const approveRes = page.waitForResponse((r) => r.url().includes("/proposal/approve") && r.request().method() === "POST");
  await approve.click();
  const ok = page.getByTestId("v2-confirm-ok");
  if (await ok.isVisible({ timeout: 3000 }).catch(() => false)) await ok.click();
  const ar = await approveRes;
  const approved = await ar.json();
  ledger(`arch-3 approve → ${ar.status()} job=${approved.job_id} agent=${approved.agent?.id} started=${approved.started}`);
  expect([200, 202]).toContain(ar.status());
  stateSet("arch_agent_id", approved.agent?.id);
  await evidence(page, "arch3-approved-deploying");
  let lastKey = "";
  const job = await waitForJob(page.request, approved.job_id, {
    timeoutMs: 25 * 60 * 1000,
    onTick: (j) => {
      const ev = (j.events as Array<{ stage: string; msg: string }>) ?? [];
      const tail = ev[ev.length - 1];
      const key = tail ? `${tail.stage}:${tail.msg}` : "";
      if (key !== lastKey) {
        lastKey = key;
        ledger(`arch-3 job ${String(approved.job_id).slice(0, 8)} ${j.status} ${key.slice(0, 140)}`);
      }
    },
  });
  const deploySeconds = Math.round((Date.now() - t1) / 1000);
  await attach(testInfo, "deploy-job.json", job);
  await page.reload();
  await expect(page.getByTestId("v2-assistant-outcome")).toBeVisible({ timeout: 60_000 });
  await evidence(page, "arch3-outcome");
  ledger(`arch-3 deploy ${job.status} in ${deploySeconds}s`);
  expect(job.status, `deploy failed: ${job.error ?? ""}`).toBe("succeeded");
  expect(deploySeconds, "active ≤ 15 min after approval").toBeLessThanOrEqual(15 * 60);
  }
  const agentId = reuseId ?? stateGet<string>("arch_agent_id")!;
  const agentName = stateGet<string>("arch_agent_name")!;
  const c = reuseId ? reuseChoices : (stateGet<{ choices: Record<string, unknown> }>("arch_proposal")!.choices);

  // the created agent is a NORMAL agent whose spec reflects the proposal
  const agent = await getAgent(page.request, agentId);
  await attach(testInfo, "agent.json", agent);
  const spec = (agent.spec ?? {}) as Record<string, unknown>;
  const specView = { name: agent.name, status: agent.status, owner: agent.owner, system_key: agent.system_key, method: spec.method, model_id: spec.model_id, tools: spec.tools, knowledge_bases: spec.knowledge_bases, memory: spec.memory, skills: spec.skills };
  ledger(`arch-3 agent=${JSON.stringify(specView).slice(0, 900)}`);
  stateSet("arch_agent_spec", specView);
  expect(agent.status).toBe("active");
  expect(agent.owner).toBe("admin");
  expect(agent.system_key ?? null).toBeNull();
  expect(spec.method).toBe("harness");
  expect(spec.model_id).toBe(c.model_id);
  const specTools = ((spec.tools ?? []) as Array<Record<string, unknown>>).map((t) => String(t.gateway_name ?? t.name ?? t.gateway_id ?? JSON.stringify(t)));
  const specKbs = ((spec.knowledge_bases ?? []) as Array<Record<string, unknown>>).map((k) => String(k.name ?? k.knowledge_base_id ?? k.kb_id ?? JSON.stringify(k)));
  const proposalTools = (c.tools ?? []) as string[];
  const proposalKbs = (c.knowledge_bases ?? []) as string[];
  ledger(`arch-3 spec tools=${JSON.stringify(specTools)} (proposal ${JSON.stringify(proposalTools)}) kbs=${JSON.stringify(specKbs)} (proposal ${JSON.stringify(proposalKbs)}) memory=${JSON.stringify(spec.memory)} (proposal ${c.memory})`);
  expect(specTools.length, "every proposed tool is on the spec").toBe(proposalTools.length);
  expect(specKbs.length, "every proposed KB is on the spec").toBe(proposalKbs.length);
  const mem = (spec.memory ?? {}) as { short_term?: boolean; long_term?: boolean };
  expect(!!mem.long_term, "long_term memory flag follows the proposal").toBe(c.memory === "workspace");

  // normal-agent affordances: delete offered on the list; no preset marker
  await page.goto("/v2/agents");
  await expect(page.getByTestId("v2-agents-table")).toBeVisible();
  const del = page.getByTestId(`v2-agent-delete-${agentName}`);
  const edit = page.getByTestId(`v2-agent-edit-${agentName}`);
  await expect(page.getByText(agentName).first(), "the new agent is listed").toBeVisible({ timeout: 60_000 });
  const delVisible = await del.isVisible().catch(() => false);
  const editVisible = await edit.isVisible().catch(() => false);
  ledger(`arch-3 agents list: delete=${delVisible} edit=${editVisible}`);
  await evidence(page, "arch3-agents-list");
  expect(delVisible, "delete offered for the assistant-created agent").toBe(true);
  await page.goto(`/v2/agents?view=detail&id=${encodeURIComponent(agentId)}`);
  await expect(page.getByTestId("v2-agent-log")).toBeVisible({ timeout: 60_000 });
  const detailText = (await page.locator("main").innerText().catch(() => "")) ?? "";
  const redeployOffered = /redeploy/i.test(detailText);
  ledger(`arch-3 agent detail: redeploy text present=${redeployOffered}`);
  await evidence(page, "arch3-agent-detail");

  // ── Chat ─────────────────────────────────────────────────────────────────
  const chat = await chatOnce(page, agentId, "A new hire cannot connect to the VPN from her laptop on day one. What should she check first, and when should she escalate?");
  await attach(testInfo, "chat-answer-1.txt", chat.answer);
  await evidence(page, "arch3-chat-1");
  ledger(`arch-3 chat1 ${chat.seconds}s tools=${JSON.stringify(chat.toolNames)} errors=${chat.errors}: ${chat.answer.slice(0, 240).replace(/\n/g, " ")}`);
  stateSet("arch_chat_session", chat.sessionId);
  expect(chat.errors).toBe(0);
  expect(looksLikeError(chat.answer)).toBe(false);
  expect(chat.answer.length).toBeGreaterThan(40);
  expect(chat.answer).toMatch(/VPN|network|connect|escalat|support/i);
  if (proposalTools.length) {
    const chat2 = await chatOnce(page, agentId, "Which department does the employee with id E1001 belong to, and who is their manager? Use the HR database.", { sessionId: chat.sessionId });
    await attach(testInfo, "chat-answer-2.txt", chat2.answer);
    await evidence(page, "arch3-chat-2-tool");
    ledger(`arch-3 chat2 ${chat2.seconds}s tools=${JSON.stringify(chat2.toolNames)} errors=${chat2.errors}: ${chat2.answer.slice(0, 240).replace(/\n/g, " ")}`);
    stateSet("arch_chat2", { tools: chat2.toolNames, errors: chat2.errors, seconds: chat2.seconds });
  }
  ledger(`arch-3 end PASS agent=${agentId} name=${agentName} chat=${chat.seconds}s`);
});

test("arch-5: the preset's invocations and the new agent's chat session appear in Observability", async ({ page }, testInfo) => {
  const agentName = stateGet<string>("arch_agent_name");
  const sessionId = stateGet<string>("arch_chat_session");
  ledger(`arch-5 start agent=${agentName} session=${sessionId}`);
  expect(agentName && sessionId, "arch-3 must have run").toBeTruthy();
  let traces: Array<Record<string, unknown>> = [];
  let sessions: Array<Record<string, unknown>> = [];
  let presetTraces: Array<Record<string, unknown>> = [];
  let presetSessions: Array<Record<string, unknown>> = [];
  let mine: Array<Record<string, unknown>> = [];
  let mySession: Record<string, unknown> | undefined;
  const t0 = Date.now();
  for (let i = 0; i < 24; i++) {
    traces = ((await (await page.request.get("/api/observability/traces?range=24h&force=true")).json()).traces ?? []) as Array<Record<string, unknown>>;
    sessions = ((await (await page.request.get("/api/observability/sessions?range=24h&force=true")).json()).sessions ?? []) as Array<Record<string, unknown>>;
    const isPreset = (r: Record<string, unknown>) => [r.agent, r.service, r.agent_name].some((v) => v === PRESET || v === PRESET_LABEL);
    presetTraces = traces.filter(isPreset);
    presetSessions = sessions.filter(isPreset);
    mine = traces.filter((r) => String(r.session_id) === sessionId || r.agent === agentName || r.service === agentName);
    mySession = sessions.find((s) => String(s.session_id) === sessionId);
    if (presetTraces.length && mine.length && mySession) break;
    await sleep(10_000);
  }
  const lag = Math.round((Date.now() - t0) / 1000);
  const agentsSeen = [...new Set(traces.map((r) => String(r.agent ?? r.service)))];
  ledger(`arch-5 after ${lag}s traces=${traces.length} preset-traces=${presetTraces.length} preset-sessions=${presetSessions.length} my-agent-traces=${mine.length} my-session-listed=${!!mySession} agents=${JSON.stringify(agentsSeen).slice(0, 400)}`);
  await attach(testInfo, "preset-traces.json", presetTraces.slice(0, 30));
  await attach(testInfo, "my-traces.json", mine.slice(0, 30));
  await attach(testInfo, "sessions.json", sessions.slice(0, 50));

  await page.goto("/v2/observability?tab=traces&range=24h");
  await page.getByTestId("v2-obs-refresh").click().catch(() => {});
  await expect(page.getByTestId("v2-obs-traces-table")).toBeVisible({ timeout: 90_000 });
  // the table is mounted while the report is still loading — wait for rows before reading the filters
  await expect(page.getByTestId("v2-obs-traces-table").getByText(/Loading/i)).toHaveCount(0, { timeout: 120_000 });
  const sysBox = page.getByTestId("v2-obs-traces-system");
  const sysBoxShown = await sysBox.isVisible().catch(() => false);
  if (sysBoxShown) await sysBox.check();
  const agentSelect = page.getByTestId("v2-obs-traces-agent");
  const options = await agentSelect.locator("option").allInnerTexts().catch(() => [] as string[]);
  const presetOption = options.find((o) => o.includes(PRESET) || o.includes(PRESET_LABEL));
  if (presetOption) await agentSelect.selectOption({ label: presetOption }).catch(() => {});
  ledger(`arch-5 traces UI: system-box=${sysBoxShown} agent-options=${JSON.stringify(options).slice(0, 300)} preset-option=${presetOption ?? "none"}`);
  await evidence(page, "arch5-traces-preset");
  if (presetOption) await agentSelect.selectOption({ index: 0 }).catch(() => {});
  const myOption = options.find((o) => o === agentName);
  if (myOption) await agentSelect.selectOption({ label: myOption }).catch(() => {});
  await evidence(page, "arch5-traces-helpdesk");
  await page.goto("/v2/observability?tab=sessions&range=24h");
  await expect(page.getByTestId("v2-obs-sessions-table")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByTestId("v2-obs-sessions-table").getByText(/Loading/i)).toHaveCount(0, { timeout: 120_000 });
  await evidence(page, "arch5-sessions");
  await page.goto(`/v2/observability?view=session&id=${encodeURIComponent(sessionId!)}&range=24h`);
  await expect(page.getByTestId("v2-obs-session-traces").or(page.getByTestId("v2-obs-session-notfound"))).toBeVisible({ timeout: 90_000 });
  const found = await page.getByTestId("v2-obs-session-traces").isVisible().catch(() => false);
  await evidence(page, "arch5-session-detail");
  ledger(`arch-5 session detail found=${found}`);
  expect(presetTraces.length, "the preset's own invocations are traced").toBeGreaterThan(0);
  expect(mine.length, "the helpdesk agent's chat is traced").toBeGreaterThan(0);
  expect(!!mySession, "the chat session is listed").toBe(true);
  ledger(`arch-5 end PASS preset-traces=${presetTraces.length} my-traces=${mine.length} lag=${lag}s`);
});

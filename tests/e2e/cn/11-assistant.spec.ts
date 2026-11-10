import { expect, test } from "@playwright/test";
import { attach, chatOnce, evidence, ledger, listAgents, looksLikeError, openClassicPresets, sleep, stateSet, waitForJob, E2E_PREFIX } from "./helpers";

const PRESET = "aws-agent-solution-architect";

/**
 * Case 11 (round 4) — Architect assistant: (install the system preset through the console
 * if it is not active) → new conversation → ≤ 3 turns produce a proposal → approve → the
 * proposed agent is active ≤ 15 min → Chat answers with model text.
 *
 * N10v3-bug4 re-check: the proposal must be recognised even when the model fences it as
 * ```json launchpad-proposal (round 3: `proposals: []` after 3 turns). The spec records the
 * fence info-strings actually emitted (from the stored conversation) next to the verdict.
 * Round-4 AuthGate attribution: `/agents/new` is opened through `openClassicPresets`, which
 * samples `data-testid=v2-auth-loading` / `data-reason` while the classic page is not mounted.
 */
test("case 11: assistant conversation → proposal ≤3 turns (json-fenced ok) → approve → active ≤15 min → Chat", async ({ page }, testInfo) => {
  ledger("case11 start");
  // ── preset availability (admin install through the product UI) ──────────
  let status = await (await page.request.get("/api/assistant/architect")).json();
  if (!status.available) {
    ledger(`case11 assistant unavailable: ${JSON.stringify(status.reasons)} preset=${status.preset?.status} — installing via console`);
    await page.goto("/v2/assistant");
    await expect(page.getByTestId("v2-assistant-unavailable")).toBeVisible({ timeout: 60_000 });
    await evidence(page, "case11-assistant-unavailable");
    const open = await openClassicPresets(page, "case11-install");
    await attach(testInfo, "classic-presets-open-install.json", open);
    stateSet("authgate_case11_install", open);
    const install = page.getByTestId(`install-${PRESET}`);
    await expect(install, `install button (panel mounted after ${open.readyMs}ms; spinner samples ${open.stall.length})`).toBeVisible({ timeout: 60_000 });
    await evidence(page, "case11-preset-before-install");
    const t0 = Date.now();
    const res = page.waitForResponse((r) => r.url().includes(`/api/system-agents/${PRESET}/install`) && r.request().method() === "POST", { timeout: 60_000 });
    await install.click();
    const dialog = page.getByRole("alertdialog");
    if (await dialog.isVisible({ timeout: 3000 }).catch(() => false)) await dialog.getByRole("button").last().click();
    const ok = page.getByTestId("v2-confirm-ok");
    if (await ok.isVisible({ timeout: 1000 }).catch(() => false)) await ok.click();
    const r = await res;
    const body = await r.json().catch(() => ({}));
    ledger(`case11 install → ${r.status()} ${JSON.stringify(body).slice(0, 200)}`);
    expect([200, 202]).toContain(r.status());
    const jobId = body.job_id ?? body.job?.id;
    if (jobId) {
      // N12-obs31 (a): the preset's job is readable through the preset route (the generic /api/jobs may 404)
      let job = await (await page.request.get(`/api/system-agents/${PRESET}/jobs/${jobId}`)).json().catch(() => null);
      const generic = await page.request.get(`/api/jobs/${jobId}`);
      ledger(`case11 install job ${jobId}: preset route status=${job?.status ?? "n/a"} generic /api/jobs → ${generic.status()}`);
      const started = Date.now();
      while (Date.now() - started < 20 * 60 * 1000) {
        job = await (await page.request.get(`/api/system-agents/${PRESET}/jobs/${jobId}`)).json().catch(() => null);
        if (job && !["pending", "queued", "running", "in_progress"].includes(String(job.status))) break;
        await sleep(15_000);
      }
      if (!job) job = await waitForJob(page.request, jobId, { timeoutMs: 20 * 60 * 1000 });
      await attach(testInfo, "preset-install-job.json", job);
      expect(job.status, `preset install failed: ${job.error ?? ""}`).toBe("succeeded");
    }
    for (let i = 0; i < 90; i++) {
      status = await (await page.request.get("/api/assistant/architect")).json();
      if (status.available) break;
      await sleep(10_000);
    }
    ledger(`case11 preset installed in ${Math.round((Date.now() - t0) / 1000)}s available=${status.available}`);
    stateSet("preset_install_seconds", Math.round((Date.now() - t0) / 1000));
    const reopen = await openClassicPresets(page, "case11-installed");
    await attach(testInfo, "classic-presets-open-after-install.json", reopen);
    stateSet("authgate_case11_after_install", reopen);
    await evidence(page, "case11-preset-installed");
  }
  expect(status.available, `assistant must be available: ${JSON.stringify(status.reasons)}`).toBe(true);

  // ── conversation → proposal within 3 turns ───────────────────────────────
  await page.goto("/v2/assistant");
  await page.getByTestId("v2-assistant-new").click();
  await expect(page.getByTestId("v2-assistant-detail")).toBeVisible({ timeout: 60_000 });
  const conversationId = await page.getByTestId("v2-assistant-detail").getAttribute("data-conversation");
  ledger(`case11 conversation=${conversationId}`);
  const stamp = new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "");
  const agentName = `${E2E_PREFIX}assist-${stamp}`;
  const prompts = [
    `Build me a tiny FAQ agent named ${agentName}. Purpose: answer questions about the fictional company Octank's office hours (9:00-18:00 CST, Monday to Friday) in one or two sentences, English only. No tools, no knowledge base, no memory, default GLM model. Please write the proposal now; no more questions needed.`,
    `Yes — all of that is final. Name ${agentName}, no tools, no KB, short-term memory off. Produce the proposal now.`,
    `Proceed with your best assumptions and output the proposal block.`,
  ];
  // a proposal block the server REJECTED (`proposal_status: invalid`) is rendered with its
  // validation errors and replayed to the assistant — the user's next message asks for the fix
  const fixPrompt = "Please fix the validation errors with a patch block; keep every confirmed decision and drop any evaluator that needs data I did not give you.";
  let turns = 0;
  const proposal = page.getByTestId("v2-assistant-proposal-view");
  const tConv = Date.now();
  let conv: Record<string, unknown> = {};
  const statuses: string[] = [];
  for (let i = 0; i < prompts.length; i++) {
    turns += 1;
    const last = statuses[statuses.length - 1];
    const p = last === "invalid" ? fixPrompt : prompts[i];
    await page.getByTestId("v2-assistant-input").fill(p);
    await page.getByTestId("v2-assistant-send").click();
    await expect(page.getByTestId("v2-assistant-streaming")).toHaveCount(0, { timeout: 6 * 60 * 1000 });
    await expect(page.getByTestId("v2-assistant-input")).toBeEnabled({ timeout: 60_000 });
    const errors = await page.getByTestId("v2-assistant-turn-error").count();
    const thread = (await page.getByTestId("v2-assistant-thread").innerText()).slice(-400).replace(/\n/g, " ");
    conv = await (await page.request.get(`/api/assistant/architect/conversations/${conversationId}`)).json();
    const st = String(conv.proposal_status ?? "none");
    statuses.push(st);
    const verr = ((conv.proposals ?? []) as Array<{ validation_errors?: unknown }>).map((x) => x.validation_errors).filter(Boolean);
    ledger(`case11 turn ${turns} done errors=${errors} proposal_status=${st} validation_errors=${JSON.stringify(verr).slice(0, 300)}: …${thread}`);
    await evidence(page, `case11-turn-${turns}`);
    if ((await proposal.isVisible().catch(() => false)) && st !== "invalid") break;
  }
  stateSet("case11_statuses", statuses);
  await attach(testInfo, "conversation.json", conv);
  // N10v3-bug4: which fence info-strings did the model actually use?
  const texts = ((conv.messages ?? []) as Array<{ role?: string; text?: string; content?: string }>).map((m) => String(m.text ?? m.content ?? ""));
  const fences = [...new Set(texts.flatMap((t) => [...t.matchAll(/```([^\n]*launchpad-proposal[^\n]*)\n/g)].map((m) => m[1].trim())))];
  const jsonFenced = fences.some((f) => /^json\b/i.test(f));
  ledger(`case11 proposal fences seen=${JSON.stringify(fences)} json-prefixed=${jsonFenced} proposals=${JSON.stringify(conv.proposals ?? []).slice(0, 120)} proposal_status=${conv.proposal_status}`);
  await attach(testInfo, "proposal-fences.json", { fences, jsonFenced, turns, seconds: Math.round((Date.now() - tConv) / 1000) });
  stateSet("case11_fences", { fences, jsonFenced, turns });
  expect(await proposal.isVisible(), `a proposal must appear within 3 turns (took ${turns}; fences ${JSON.stringify(fences)}; statuses ${statuses.join("→")})`).toBe(true);
  expect(statuses[statuses.length - 1], `the proposal must be VALID (approvable) within 3 turns (statuses ${statuses.join("→")})`).not.toBe("invalid");
  expect(turns).toBeLessThanOrEqual(3);
  const proposedName = (await page.getByTestId("v2-assistant-proposal-name").innerText()).trim();
  ledger(`case11 proposal after ${turns} turn(s) in ${Math.round((Date.now() - tConv) / 1000)}s name=${proposedName} revision=${await proposal.getAttribute("data-revision")}`);
  await evidence(page, "case11-proposal");

  // ── approve → deploy ─────────────────────────────────────────────────────
  const approve = page.getByTestId("v2-assistant-proposal-approve");
  await expect(approve).toBeEnabled({ timeout: 60_000 });
  const t1 = Date.now();
  const approveRes = page.waitForResponse((r) => r.url().includes("/proposal/approve") && r.request().method() === "POST");
  await approve.click();
  const ok = page.getByTestId("v2-confirm-ok");
  if (await ok.isVisible({ timeout: 3000 }).catch(() => false)) await ok.click();
  const ar = await approveRes;
  const approved = await ar.json();
  ledger(`case11 approve → ${ar.status()} job=${approved.job_id} agent=${approved.agent?.id}`);
  expect([200, 202]).toContain(ar.status());
  const job = await waitForJob(page.request, approved.job_id, { timeoutMs: 25 * 60 * 1000 });
  const deploySeconds = Math.round((Date.now() - t1) / 1000);
  await attach(testInfo, "deploy-job.json", job);
  await page.reload();
  await expect(page.getByTestId("v2-assistant-outcome")).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case11-outcome");
  expect(job.status, `deploy failed: ${job.error ?? ""}`).toBe("succeeded");
  expect(deploySeconds, "active ≤ 15 min after approval").toBeLessThanOrEqual(15 * 60);
  const agent = (await listAgents(page.request)).find((a) => a.id === approved.agent.id);
  expect(agent?.status).toBe("active");

  const chat = await chatOnce(page, String(approved.agent.id), "What are Octank's office hours?");
  await attach(testInfo, "chat-answer.txt", chat.answer);
  await evidence(page, "case11-chat");
  ledger(`case11 chat ${chat.seconds}s: ${chat.answer.slice(0, 200).replace(/\n/g, " ")}`);
  expect(chat.errors).toBe(0);
  expect(looksLikeError(chat.answer)).toBe(false);
  expect(chat.answer).toMatch(/9|18|Monday|Friday|hours/i);
  ledger(`case11 end PASS agent=${approved.agent.id} name=${agent?.name} turns=${turns} statuses=${statuses.join("→")} deploy=${deploySeconds}s fences=${JSON.stringify(fences)}`);
});

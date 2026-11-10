import { expect, test } from "@playwright/test";
import { chatOnce, createStrandsViaWizard, e2eName, getAgent, ledger, listAgents, looksLikeError, waitForDeploy } from "./helpers";
import { N12E_PREFIX, SMOKE_PROMPTS, record, snap } from "./n12e";

/**
 * N12 campaign precondition: one Launchpad-managed Strands HTTP agent (`zip_runtime`) named
 * `n12e-smoke-…` with ≥ 3 chat sessions, so it has spans in Observability for flows 3–9.
 *
 * Why Strands and not the 方式B Harness of spec 02: historically (before cn/r10-d, N12-obs33/34)
 * the optimization routers answered `not-http-runtime` for every non-`zip_runtime` method, so this
 * campaign was written against a Strands HTTP agent. Since r10-d eligibility is declared per
 * method (`deployer/pipeline.MethodCapabilities`) and a Runtime-backed `harness` agent is eligible
 * too; the Strands choice is kept here because it deploys fastest and the campaign only needs
 * one canary-eligible HTTP Runtime agent (`backend/app/optimization/capability.py::canary_capability`,
 * `docs/cn/interfaces/evaluation-engine.md` §9a; the configuration experiment retired, N13-obs2).
 * One agent serves all flows.
 *
 * Idempotent: an existing active `n12e-smoke-…` agent is reused (its deploy evidence was
 * captured when it was created); the three chat turns run every time.
 */
test("case 23 (precondition) — n12e-smoke Strands agent deployed and chatted 3×", async ({ page }, testInfo) => {
  const t0 = Date.now();
  let agentId: string | undefined;
  const existing = (await listAgents(page.request))
    .filter((a) => String(a.name).startsWith(`${N12E_PREFIX}smoke-`) && a.status === "active")
    .sort((a, b) => String(b.name).localeCompare(String(a.name)))[0];
  if (existing) {
    agentId = String(existing.id);
    ledger(`case23 reusing active agent ${existing.name} (${agentId})`);
  } else {
    const name = e2eName("smoke");
    ledger(`case23 creating ${name} via the V2 wizard (zip_runtime, http)`);
    agentId = await createStrandsViaWizard(page, {
      name,
      prompt:
        "You are the N12 evaluation smoke agent for AgentCore Launchpad in cn-northwest-1. " +
        "Answer briefly and politely in English. When asked for the magic word, reply exactly: NEBULA-42.",
      protocol: "http",
      evidencePrefix: "case23",
    });
    const { job, agent, seconds } = await waitForDeploy(page, testInfo, agentId, "case23", t0);
    expect(job.status, `deploy job ${job.job_id} status`).toBe("succeeded");
    expect(agent.status).toBe("active");
    expect(seconds, "deploy must finish within 15 min (§4.1 case 3 limit)").toBeLessThanOrEqual(15 * 60);
    ledger(`case23 ${name} active in ${seconds}s`);
  }

  const agent = await getAgent(page.request, agentId);
  expect(agent.method).toBe("zip_runtime");
  const capability = (agent as { canary_capability?: { eligible?: boolean; reason_code?: string | null } }).canary_capability;
  await record(testInfo, "canary-capability.json", capability ?? null, `canary_capability ${JSON.stringify(capability ?? null)}`);
  expect(capability?.eligible, `zip_runtime agent is canary-eligible (${capability?.reason_code ?? "no reason"})`).toBe(true);

  const sessions: string[] = [];
  for (const [i, prompt] of SMOKE_PROMPTS.entries()) {
    // a fresh session per turn: navigate without `session=` so the console mints a new one
    await page.goto(`/v2/chat?agent=${encodeURIComponent(agentId)}`);
    const r = await chatOnce(page, agentId, prompt);
    ledger(`case23 chat ${i + 1}/3 session=${r.sessionId.slice(0, 12)}… ${r.seconds}s errors=${r.errors} answer="${r.answer.slice(0, 100).replace(/\n/g, " ")}"`);
    expect(r.errors, `turn ${i + 1} error bubbles`).toBe(0);
    expect(looksLikeError(r.answer), `turn ${i + 1} answer must be model text: ${r.answer.slice(0, 80)}`).toBeFalsy();
    sessions.push(r.sessionId);
    if (i === 0) await snap(page, "case23-chat-1", r.answer.slice(0, 60));
  }
  await snap(page, "case23-chat-3");
  await record(testInfo, "sessions.json", { agent_id: agentId, name: agent.name, sessions }, `3 sessions ${sessions.map((s) => s.slice(0, 8)).join(",")}`);
  expect(new Set(sessions).size).toBe(3);
});

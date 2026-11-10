import { expect, test } from "@playwright/test";
import { attach, chatOnce, createHarnessViaWizard, e2eName, evidence, ledger, looksLikeError, memoryRailEvents, reusableAgent, waitForDeploy, type DeployResult } from "./helpers";

/**
 * Case 2 — 方式B (self-built Harness): wizard → deploy reaches `active` within 15 min →
 * Chat streams real model text (GLM 5.3 via the Global Bedrock API-key provider; not an
 * echo, not an error turn) → the Memory rail shows ≥ 1 short-term event for this
 * session WITHOUT a reload (N11 r2 memory-rail settle poll).
 * Long-term memory is switched on so case 6 can reuse this agent; it is the one
 * deployment kept at the end (observability evidence).
 */
test("case 2: Harness wizard → deploy active ≤15min → Chat streams GLM text → memory rail ≥1 event without reload", async ({ page }, testInfo) => {
  const name = e2eName("harness");
  ledger(`case2 start name=${name}`);
  let agentId = "";
  let dep: DeployResult = { seconds: 0 } as DeployResult;
  const reuse = await reusableAgent(page.request, "harness");
  if (reuse) {
    agentId = reuse.id;
    ledger(`case2 reusing active agent ${reuse.name} (${agentId})`);
  } else {
    await page.goto("/v2/agents");
    await page.getByTestId("v2-agent-new").click();
    await expect(page.getByTestId("v2-agent-method-harness")).toBeVisible();
    await evidence(page, "case02-wizard-method-card");
    const t0 = Date.now();
    agentId = await createHarnessViaWizard(page, {
      name,
      prompt:
        "You are an e2e test agent for AgentCore Launchpad in cn-northwest-1. Answer briefly in English. " +
        "When asked for a secret phrase, reply exactly: LAUNCHPAD-CN-OK. Remember personal facts the user shares.",
      longTerm: true,
      evidencePrefix: "case02",
    });
    ledger(`case2 agent created id=${agentId}`);
    // default model must be GLM 5.3 (asserted on the stored spec)
    dep = await waitForDeploy(page, testInfo, agentId, "case2", t0);
    expect(dep.job.status, `deploy job failed: ${dep.job.error ?? ""}`).toBe("succeeded");
    expect(dep.agent.status).toBe("active");
    expect(dep.seconds, "deploy must reach active within 15 min").toBeLessThanOrEqual(15 * 60);
    const spec = (dep.agent.spec ?? {}) as { model_id?: string; memory?: { long_term?: boolean } };
    expect(String(spec.model_id)).toMatch(/glm/i);
    expect(spec.memory?.long_term).toBe(true);
  }

  // ── chat: stream an answer (no navigation afterwards — the rail must update in place) ─
  await page.goto(`/v2/chat?agent=${encodeURIComponent(agentId)}`);
  await expect(page.getByTestId("chat-memory-state")).toHaveAttribute("data-state", "on");
  const chat = await chatOnce(page, agentId, "What is the secret phrase? After it, add one short sentence about what 2+2 equals.");
  await attach(testInfo, "chat-answer.txt", chat.answer);
  ledger(`case2 chat answered in ${chat.seconds}s errors=${chat.errors}: ${chat.answer.slice(0, 200).replace(/\n/g, " ")}`);
  expect(chat.errors, "no error bubble in the turn").toBe(0);
  expect(looksLikeError(chat.answer), "answer must be model text, not echo/error").toBe(false);
  expect(chat.answer).toMatch(/LAUNCHPAD-CN-OK/i);
  expect(chat.answer).toMatch(/4|four/i);
  await evidence(page, "case02-chat-stream");

  // ── memory rail: ≥ 1 event, read from the DOM without any reload/navigation ─
  const urlBefore = page.url();
  const events = await memoryRailEvents(page, 90_000);
  expect(page.url(), "no navigation happened while waiting for the rail").toBe(urlBefore);
  await evidence(page, "case02-memory-panel-no-reload");
  ledger(`case2 memory rail (no reload) events=${events}`);
  expect(events, "memory rail short-term events ≥ 1 without reload").toBeGreaterThanOrEqual(1);
  await expect(page.getByTestId("open-in-memory")).toBeVisible();
  const api = await (await page.request.get(`/api/chat/${agentId}/memory?session_id=${encodeURIComponent(chat.sessionId)}`)).json();
  await attach(testInfo, "memory-summary.json", api);
  expect(api.event_count ?? 0).toBeGreaterThanOrEqual(1);
  ledger(`case2 end PASS agent=${agentId} session=${chat.sessionId} events=${events} deploy=${dep.seconds}s chat=${chat.seconds}s`);
});

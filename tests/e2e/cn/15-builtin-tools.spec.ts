import { expect, test } from "@playwright/test";
import { attach, chatOnce, evidence, getAgent, ledger, reusableAgent, waitForDeploy, e2eName } from "./helpers";

/** Case 15 — built-in tools: a Harness agent with the Code Interpreter solves an arithmetic task; the tool call is visible and the number is exact. */
test("case 15: Code Interpreter agent → tool call visible → exact numeric result", async ({ page }, testInfo) => {
  const name = e2eName("ci");
  ledger(`case15 start name=${name}`);
  let agentId = "";
  const reuse = await reusableAgent(page.request, "ci");
  if (reuse) agentId = reuse.id;
  else {
    await page.goto("/v2/agents");
    await page.getByTestId("v2-agent-new").click();
    await page.getByTestId("v2-agent-method-harness").click();
    await page.getByTestId("v2-agent-wizard-next").click();
    await page.getByTestId("v2-agent-name").fill(name);
    await page.getByTestId("v2-agent-prompt").fill(
      "You are a calculator assistant. For ANY arithmetic question you MUST run Python in the code interpreter tool to compute the exact value, then answer with the exact integer result.",
    );
    const box = page.getByTestId("v2-agent-builtins").locator("label", { hasText: /code interpreter/i }).locator('input[type="checkbox"]');
    await expect(box).toBeVisible();
    await box.check();
    await evidence(page, "case15-wizard-configure");
    await page.getByTestId("v2-agent-wizard-next").click();
    await expect(page.getByTestId("v2-agent-review")).toBeVisible();
    const t0 = Date.now();
    await page.getByTestId("v2-agent-wizard-submit").click();
    await expect(page).toHaveURL(/view=detail&id=/, { timeout: 60_000 });
    agentId = new URL(page.url()).searchParams.get("id")!;
    const spec = (await getAgent(page.request, agentId)).spec as { tools?: Array<{ type: string; name: string }> };
    expect(spec.tools?.some((t) => t.type === "builtin" && /code-interpreter/.test(t.name)), "spec carries the builtin tool").toBe(true);
    const dep = await waitForDeploy(page, testInfo, agentId, "case15", t0);
    expect(dep.job.status, `deploy failed: ${dep.job.error ?? ""}`).toBe("succeeded");
    expect(dep.seconds).toBeLessThanOrEqual(15 * 60);
  }
  // 2**20 + 12345 = 1060921 ; 123456789 * 987 = 121851850743
  const chat = await chatOnce(page, agentId, "Use the code interpreter to compute 123456789 * 987 and 2**20 + 12345. Reply with both exact integers.");
  await attach(testInfo, "chat-answer.txt", chat.answer);
  await evidence(page, "case15-code-interpreter-chat");
  ledger(`case15 chat ${chat.seconds}s tools=${chat.toolNames.join(",")}: ${chat.answer.slice(0, 240).replace(/\n/g, " ")}`);
  expect(chat.errors).toBe(0);
  expect(chat.toolCalls, "tool call visible").toBeGreaterThanOrEqual(1);
  expect(chat.toolNames.join(" ")).toMatch(/code|interpreter|execute|python/i);
  const digits = chat.answer.replace(/[,\s]/g, "");
  expect(digits).toMatch(/121851850743/);
  expect(digits).toMatch(/1060921/);
  ledger(`case15 end PASS agent=${agentId}`);
});

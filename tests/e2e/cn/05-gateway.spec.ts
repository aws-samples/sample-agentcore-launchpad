import { expect, test } from "@playwright/test";
import { attach, chatOnce, createHarnessViaWizard, e2eName, evidence, ledger, registryRecords, reusableAgent, waitForDeploy } from "./helpers";

/**
 * Case 5 — Gateway: the wizard offers `hr-database` directly (no sync-defaults — the
 * N11 group-B bootstrap auto-registers + approves the gateway targets); an agent with
 * it attached answers an employee question with ≥ 1 tool call and the fixture's name
 * and department.
 */
test("case 5: hr-database attachable out of the box; tool-backed answer names the employee and department", async ({ page }, testInfo) => {
  test.skip(!process.env.N10_GROUP_B, "awaiting group B (gateway targets auto-registered + APPROVED at bootstrap)");
  const name = e2eName("gw");
  ledger(`case5 start name=${name}`);
  // precondition without any workaround: the records exist and are APPROVED
  const records = await registryRecords(page.request);
  const hr = records.find((r) => r.name === "hr-database");
  await attach(testInfo, "registry-records.json", records);
  expect(hr, "hr-database record must exist after bootstrap (no sync-defaults)").toBeTruthy();
  expect(hr!.status).toBe("APPROVED");
  let agentId = "";
  const reuse = await reusableAgent(page.request, "gw");
  if (reuse) agentId = reuse.id;
  else {
    const t0 = Date.now();
    agentId = await createHarnessViaWizard(page, {
      name,
      prompt: "You are an HR assistant for Octank. Use the hr-database tools to answer employee questions; always call get_employee for an employee id and quote the returned fields.",
      gateways: ["hr-database"],
      evidencePrefix: "case05",
    });
    const dep = await waitForDeploy(page, testInfo, agentId, "case5", t0);
    expect(dep.job.status, `deploy job failed: ${dep.job.error ?? ""}`).toBe("succeeded");
    expect(dep.seconds).toBeLessThanOrEqual(15 * 60);
  }
  const q = await chatOnce(page, agentId, "Look up employee EMP-1024: what are their name, department and remaining vacation days this year?");
  await attach(testInfo, "case5-answer.txt", q.answer);
  await evidence(page, "case05-gateway-chat");
  ledger(`case5 chat ${q.seconds}s tools=${q.toolNames.join(",")}: ${q.answer.slice(0, 240).replace(/\n/g, " ")}`);
  expect(q.toolCalls, "≥ 1 gateway tool call").toBeGreaterThanOrEqual(1);
  expect(q.toolNames.join(" ")).toMatch(/hr-database___get_employee/);
  // fixture: samples/hr_database_lambda/handler.py EMP-1024 = Maya Chen · HR Operations
  expect(q.answer).toMatch(/Maya Chen/);
  expect(q.answer).toMatch(/HR Operations/);
  ledger(`case5 end PASS agent=${agentId} session=${q.sessionId}`);
});

import { expect, test } from "@playwright/test";
import { execSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { attach, awsRead, chatOnce, createByocViaWizard, e2eName, evidence, getAgent, ledger, looksLikeError, REPO_ROOT, reusableAgent, waitForDeploy, type DeployResult } from "./helpers";

/** Case 4 — BYOC code zip (samples/byoc/hello-http) → upload → deploy → Chat. */
test("case 4: BYOC zip upload → deploy → Chat answers", async ({ page }, testInfo) => {
  const name = e2eName("byoc");
  ledger(`case4 start name=${name}`);
  let agentId = "";
  let dep: DeployResult = { seconds: 0 } as DeployResult;
  const reuse = await reusableAgent(page.request, "byoc");
  if (reuse) {
    agentId = reuse.id;
    ledger(`case4 reusing active agent ${reuse.name} (${agentId}) — deploy evidence from the earlier run`);
  } else {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "n10-byoc-"));
  const zip = path.join(tmp, "hello-http.zip");
  execSync(`zip -qr "${zip}" .`, { cwd: path.join(REPO_ROOT, "samples/byoc/hello-http") });
  const t0 = Date.now();
  agentId = await createByocViaWizard(page, { name, zip, entrypoint: "main.py", evidencePrefix: "case04" });
  ledger(`case4 agent created id=${agentId}`);

  dep = await waitForDeploy(page, testInfo, agentId, "case4", t0);
  expect(dep.job.status, `deploy job failed: ${dep.job.error ?? ""}`).toBe("succeeded");
  expect(dep.agent.status).toBe("active");
  }

  const chat = await chatOnce(page, agentId, "Hello from the N10 e2e suite. Reply with any short sentence.");
  await attach(testInfo, "chat-answer.txt", chat.answer);
  ledger(`case4 chat answered in ${chat.seconds}s: ${chat.answer.slice(0, 200).replace(/\n/g, " ")}`);
  await evidence(page, "case04-chat");
  expect(chat.answer.length).toBeGreaterThan(5);
  ledger(`case4 answer path: ${/\[echo/.test(chat.answer) ? "ECHO fallback (model unavailable from runtime)" : "model"}`);
  // §4.1: real model text (CN-aware sample) and no EndpointConnectionError in the runtime log (read-only)
  const arn = String((await getAgent(page.request, agentId)).arn ?? "");
  const runtimeId = arn.split("/").pop() ?? "";
  const logGroup = `/aws/bedrock-agentcore/runtimes/${runtimeId}-DEFAULT`;
  const since = Date.now() - 30 * 60 * 1000;
  const log = awsRead(`logs filter-log-events --log-group-name ${logGroup} --start-time ${since} --filter-pattern EndpointConnectionError --max-items 5`);
  const hits = (() => { try { return (JSON.parse(log).events ?? []).length; } catch { return log.startsWith("ERROR") ? -1 : 0; } })();
  ledger(`case4 runtime log ${logGroup} EndpointConnectionError events=${hits} ${hits < 0 ? log.slice(0, 160) : ""}`);
  await attach(testInfo, "runtime-log-filter.txt", log.slice(0, 4000));
  expect(chat.errors, "no error bubble").toBe(0);
  expect(chat.answer).not.toMatch(/^\[echo/);
  expect(looksLikeError(chat.answer), "answer must be model text").toBe(false);
  expect(hits, "no EndpointConnectionError in the runtime log").toBe(0);
  ledger(`case4 end PASS agent=${agentId} session=${chat.sessionId} deploy=${dep.seconds}s`);
});

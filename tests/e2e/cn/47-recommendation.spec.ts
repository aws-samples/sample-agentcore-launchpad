import { expect, test } from "@playwright/test";
import { getAgent, ledger, waitForJob } from "./helpers";
import { JUDGE_DEFAULT, pollUntil, record, smokeAgent, snap } from "./n12e";

/**
 * §4.1 case 30 / plan §E.3 flow 7 — Recommendation.
 * Task detail of the completed batch run → RECOMMEND (provider `gepa_lite`, GLM 5.3) →
 * `COMPLETED` with a recommended prompt + explanation → ACCEPT → a redeploy job appears on the
 * agent (version +1 once it succeeds). N9 probe (d): is `harness_optimizer` offered?
 */
test("case 30 — recommendation (gepa_lite / GLM) → COMPLETED → ACCEPT → redeploy job", async ({ page }, testInfo) => {
  test.setTimeout(45 * 60 * 1000);
  const t0 = Date.now();
  const agent = await smokeAgent(page.request);
  const runs = ((await (await page.request.get("/api/eval/runs")).json()).runs as Array<Record<string, unknown>>)
    .filter((r) => r.agent_id === agent.id && r.status === "completed" && r.mode !== "insights");
  expect(runs.length, "a completed evaluators run of the smoke agent (spec 43)").toBeGreaterThan(0);
  const run = runs[0];
  const runId = String(run.id);
  ledger(`case30 run ${runId} (${run.name ?? run.dataset_name})`);

  // N9 probe (d): providers offered to the console
  const providers = await (await page.request.get("/api/eval/recommendation-providers")).json();
  const hoProvider = (providers.providers as Array<{ id: string; enabled: boolean }>).find((p) => p.id === "harness_optimizer");
  ledger(`case30 N9(d) providers: ${(providers.providers as Array<{ id: string; enabled: boolean }>).map((p) => `${p.id}(enabled=${p.enabled})`).join(", ")}`);
  await record(testInfo, "providers.json", providers);

  // recommendation inputs (current prompt) + eligibility
  const inputs = await (await page.request.get(`/api/eval/runs/${runId}/recommendation-inputs`)).json();
  await record(testInfo, "recommendation-inputs.json", inputs, `eligible=${inputs.eligible ?? inputs.eligibility?.eligible} source=${inputs.input_source ?? inputs.source}`);
  const currentPrompt = String(inputs.system_prompt ?? inputs.inputs?.system_prompt ?? ((await getAgent(page.request, agent.id)).spec as Record<string, unknown> | undefined)?.system_prompt ?? "");
  expect(currentPrompt.length).toBeGreaterThan(10);

  // task detail → recommendation card
  await page.goto(`/v2/eval/tasks?view=detail&id=${runId}`);
  // with no earlier recommendation the card opens the form directly; otherwise a "new" button shows it
  const form = page.getByTestId("v2-rec-form");
  const newBtn = page.getByTestId("v2-rec-new");
  await expect(form.or(newBtn).first(), "RECOMMEND available on the completed run").toBeVisible({ timeout: 60_000 });
  if (!(await form.isVisible().catch(() => false))) await newBtn.click();
  await expect(form).toBeVisible();
  const providerTrigger = page.getByTestId("v2-rec-provider");
  let uiProviders: string[] = [];
  if (await providerTrigger.isVisible().catch(() => false)) {
    await providerTrigger.click();
    uiProviders = (await page.getByRole("listbox").getByRole("option").allInnerTexts()).map((s) => s.trim());
    await page.getByRole("listbox").getByRole("option", { name: /GEPA/i }).first().click();
  }
  ledger(`case30 UI provider options: ${uiProviders.join(" | ") || "(single provider, no picker)"}; harness_optimizer api enabled=${hoProvider?.enabled}`);
  const sp = page.getByTestId("v2-rec-kind-sp");
  if (!(await sp.isChecked().catch(() => false))) await sp.click().catch(() => {});
  const td = page.getByTestId("v2-rec-kind-td");
  if (await td.isChecked().catch(() => false)) await td.click().catch(() => {});
  const promptBox = page.getByTestId("v2-rec-prompt");
  if (await promptBox.isVisible().catch(() => false) && !(await promptBox.inputValue()).trim()) await promptBox.fill(currentPrompt);
  await snap(page, "case30-recommend-form", uiProviders.join("|"));
  const before = ((await (await page.request.get(`/api/eval/runs/${runId}/recommendations`)).json()).recommendations ?? []) as Array<{ id: string }>;
  await page.getByTestId("v2-rec-submit").click();
  const tSubmit = Date.now();

  // poll until COMPLETED / FAILED
  const { body: list, seconds } = await pollUntil<{ recommendations: Array<Record<string, unknown>> }>(
    page.request,
    `/api/eval/runs/${runId}/recommendations`,
    (b) => (b.recommendations ?? []).some((r) => !before.some((x) => x.id === r.id) && ["COMPLETED", "FAILED"].includes(String(r.status))),
    { timeoutMs: 25 * 60 * 1000, intervalMs: 15_000, label: "case30 rec", key: (b) => JSON.stringify((b.recommendations ?? []).map((r) => `${String(r.id).slice(0, 8)}:${r.status}`)) },
  );
  const rec = (list.recommendations ?? []).find((r) => !before.some((x) => x.id === r.id))!;
  await record(testInfo, "recommendation.json", rec, `rec ${rec.id} ${rec.status} after ${seconds}s provider=${rec.name}`);
  const result = (rec.result ?? {}) as Record<string, unknown>;
  const recommended = String(result.recommended_prompt ?? result.system_prompt ?? result.prompt ?? "");
  ledger(`case30 rec ${rec.id} ${rec.status} in ${seconds}s prompt_len=${recommended.length} explanation=${String(result.explanation ?? result.rationale ?? "").slice(0, 160).replace(/\n/g, " ")} error=${rec.error ?? ""}`);
  await page.goto(`/v2/eval/tasks?view=detail&id=${runId}`);
  await expect(page.getByTestId("v2-rec-card").first()).toBeVisible({ timeout: 60_000 });
  await snap(page, "case30-recommendation-completed", String(rec.status));
  expect(rec.status, `recommendation ended ${rec.status}: ${rec.error ?? ""}`).toBe("COMPLETED");
  expect(recommended.length, "recommended prompt present").toBeGreaterThan(20);
  expect(String(result.explanation ?? result.rationale ?? "").length, "explanation present").toBeGreaterThan(10);

  // ACCEPT → redeploy job
  const agentBefore = await getAgent(page.request, agent.id);
  const depsBefore = (agentBefore.deployments as Array<{ job_id: string }>).map((d) => d.job_id);
  await page.getByTestId("v2-rec-accept").first().click();
  const confirm = page.getByTestId("v2-rec-accept-confirm");
  await expect(confirm).toBeVisible({ timeout: 10_000 });
  await snap(page, "case30-accept-dialog");
  await confirm.click();
  await page.waitForTimeout(3000);
  let agentAfter = await getAgent(page.request, agent.id);
  const newJob = (agentAfter.deployments as Array<{ job_id: string; status: string }>).find((d) => !depsBefore.includes(d.job_id));
  const recAfter = ((await (await page.request.get(`/api/eval/runs/${runId}/recommendations`)).json()).recommendations as Array<Record<string, unknown>>).find((r) => r.id === rec.id)!;
  ledger(`case30 accept → new deployment job ${newJob?.job_id ?? "none"} (${newJob?.status}); recommendation.accepted=${JSON.stringify(recAfter.accepted).slice(0, 160)}`);
  expect(newJob, "a redeploy job appears on the agent").toBeTruthy();
  expect(recAfter.accepted, "acceptance recorded on the recommendation").toBeTruthy();
  await page.goto(`/v2/agents?view=detail&id=${agent.id}`);
  await snap(page, "case30-agent-redeploying", newJob!.job_id.slice(0, 8));

  const job = await waitForJob(page.request, newJob!.job_id, { timeoutMs: 20 * 60 * 1000, onTick: (j) => ledger(`case30 redeploy ${newJob!.job_id.slice(0, 8)} ${j.status} ${JSON.stringify((j.events as unknown[])?.slice(-1)).slice(0, 160)}`) });
  agentAfter = await getAgent(page.request, agent.id);
  const redeploySeconds = Math.round((Date.now() - tSubmit) / 1000);
  await record(testInfo, "redeploy-job.json", job, `redeploy ${job.status}; agent status=${agentAfter.status} version ${agentBefore.version} → ${agentAfter.version}`);
  await page.goto(`/v2/agents?view=detail&id=${agent.id}`);
  await snap(page, "case30-agent-after-accept", `v${agentAfter.version} ${agentAfter.status}`);
  expect(job.status, `redeploy job ${job.job_id} ended ${job.status}`).toBe("succeeded");
  expect(agentAfter.status).toBe("active");
  expect(Number(agentAfter.version)).toBe(Number(agentBefore.version) + 1);
  const promptNow = String((agentAfter.spec as Record<string, unknown>)?.system_prompt ?? "");
  expect(promptNow).not.toBe(currentPrompt);
  ledger(`case30 PASS rec ${seconds}s, redeploy ${redeploySeconds}s total; judge ${JUDGE_DEFAULT}; ${Math.round((Date.now() - t0) / 1000)}s`);
});

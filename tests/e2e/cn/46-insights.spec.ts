import { expect, test } from "@playwright/test";
import { e2eName, ledger } from "./helpers";
import { campaignDataset, createTaskViaWizard, record, smokeAgent, snap, waitForRun, type EvalRun } from "./n12e";

/**
 * §4.1 case 29 / plan §E.3 flow 5 — Insights run.
 * Run A (the flow): new task `mode=insights` on the same dataset. Run B (fallback when the
 * dataset replay fails): `mode=insights` over the agent's recent sessions (time window).
 * `/v2/eval/insights` must then show failure clusters (or "no failures") and intent clusters.
 */
async function finish(page: import("@playwright/test").Page, testInfo: import("@playwright/test").TestInfo, runId: string, label: string) {
  const { run, seconds, transitions } = await waitForRun(page.request, runId, { label, timeoutMs: 35 * 60 * 1000 });
  await record(testInfo, `${label}-run-final.json`, run, `final ${run.status} after ${seconds}s`);
  await record(testInfo, `${label}-transitions.txt`, transitions.join("\n"));
  ledger(`${label} final=${run.status} ${Math.round(seconds / 6) / 10} min error=${String(run.error ?? "").slice(0, 200)} insights_keys=${Object.keys((run.insights ?? {}) as object).join(",")}`);
  await page.goto(`/v2/eval/tasks?view=detail&id=${runId}`);
  await page.waitForTimeout(2500);
  await snap(page, `${label}-task-final`, run.status);
  return { run, seconds };
}

test("case 29 — insights run (dataset, then trace window) → clusters on the overview page", async ({ page }, testInfo) => {
  test.setTimeout(80 * 60 * 1000);
  const t0 = Date.now();
  const agent = await smokeAgent(page.request);
  const ds = await campaignDataset(page.request);

  let runA: EvalRun | undefined;
  try {
    const id = await createTaskViaWizard(page, { name: e2eName("insights"), agentName: agent.name, mode: "insights", source: "dataset", datasetName: ds.name, evidencePrefix: "case29a" });
    runA = (await finish(page, testInfo, id, "case29a")).run;
  } catch (err) {
    ledger(`case29a insights on the dataset could not be submitted: ${String((err as Error).message).slice(0, 300)}`);
  }
  expect.soft(runA?.status, `run A (insights on dataset) ended ${runA?.status}: ${runA?.error ?? "not submitted"}`).toBe("completed");

  let done = runA?.status === "completed" ? runA : undefined;
  if (!done) {
    const idB = await createTaskViaWizard(page, { name: e2eName("insightsw"), agentName: agent.name, mode: "insights", source: "window", evidencePrefix: "case29b" });
    const b = await finish(page, testInfo, idB, "case29b");
    expect(b.run.status, `run B (insights on window) ended ${b.run.status}: ${b.run.error ?? ""}`).toBe("completed");
    done = b.run;
  }
  const insights = (done.insights ?? {}) as Record<string, unknown>;
  ledger(`case29 insights (${done.id}): ${JSON.stringify(insights).slice(0, 600)}`);
  expect(Object.keys(insights).length, "run.insights populated").toBeGreaterThan(0);

  await page.goto("/v2/eval/insights");
  await expect(page.getByTestId("v2-insights-panel")).toBeVisible({ timeout: 60_000 });
  await page.waitForTimeout(3000);
  const panelText = (await page.getByTestId("v2-insights-panel").innerText()).replace(/\s+/g, " ");
  ledger(`case29 insights panel: ${panelText.slice(0, 400)}`);
  await snap(page, "case29-insights-overview", panelText.slice(0, 80));
  expect(panelText).not.toMatch(/No completed insights task/i);
  ledger(`case29 ${runA?.status === "completed" ? "PASS" : "PARTIAL"} runA=${runA?.id ?? "-"}:${runA?.status ?? "not submitted"} done=${done.id}; total ${Math.round((Date.now() - t0) / 1000)}s`);
});

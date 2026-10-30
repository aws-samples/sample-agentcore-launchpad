import { expect, test } from "@playwright/test";
import { e2eName, ledger } from "./helpers";
import { campaignDataset, campaignJudge, createTaskViaWizard, evaluators, record, smokeAgent, snap, waitForRun, type EvalRun } from "./n12e";

/**
 * §4.1 case 26 / plan §E.3 flow 3 — Batch run on the evaluation worker.
 *
 * Run A (the §E.3 flow): `/v2/eval/tasks?view=new`, agent = n12e-smoke, dataset = n12e-ds,
 * evaluators Builtin.GoalSuccessRate, Builtin.Helpfulness, Builtin.TrajectoryInOrderMatch,
 * n12e_cn_tone (+ n12e_help_kimi on Kimi K3). Expected `queued → invoking → waiting → evaluating
 * → completed`, scores table populated, RESULTS rows with explanations. §4.1: scores ≤ 30 min.
 *
 * Run B (same wizard, source "Traces · agent sessions in a time window"): no dataset replay —
 * the worker only collects spans and judges. It is the fallback that keeps flows 7/8 testable
 * when run A fails, and it exercises the worker's evaluate phase on its own.
 */
const BUILTINS_A = ["Builtin.GoalSuccessRate", "Builtin.Helpfulness", "Builtin.TrajectoryInOrderMatch"];
const BUILTINS_B = ["Builtin.Helpfulness", "Builtin.Coherence"];

async function finish(page: import("@playwright/test").Page, testInfo: import("@playwright/test").TestInfo, runId: string, label: string) {
  let midShot = false;
  const { run, seconds, transitions } = await waitForRun(page.request, runId, {
    label,
    timeoutMs: 35 * 60 * 1000,
    onTick: (r: EvalRun) => {
      if (!midShot && ["invoking", "waiting", "evaluating"].includes(String(r.status))) {
        midShot = true;
        void page.goto(`/v2/eval/tasks?view=detail&id=${runId}`).then(() => snap(page, `${label}-task-${r.status}`, String(r.progress ?? "")));
      }
    },
  });
  await record(testInfo, `${label}-run-final.json`, run, `final ${run.status} after ${seconds}s`);
  await record(testInfo, `${label}-transitions.txt`, transitions.join("\n"));
  const statuses = Array.from(new Set(transitions.map((l) => l.split(" ").slice(3).join(" ").split("|")[0])));
  ledger(`${label} statuses seen: ${statuses.join(" → ")} · ${Math.round(seconds / 6) / 10} min · final=${run.status} error=${String(run.error ?? "").slice(0, 200)}`);
  await page.goto(`/v2/eval/tasks?view=detail&id=${runId}`);
  await page.waitForTimeout(2500);
  await snap(page, `${label}-task-final`, `${run.status} ${Math.round(seconds)}s`);
  return { run, seconds, statuses };
}

test("case 26 — batch run: dataset replay (flow 3) and trace-window run → completed with scores ≤ 30 min", async ({ page }, testInfo) => {
  test.setTimeout(80 * 60 * 1000);
  const t0 = Date.now();
  const agent = await smokeAgent(page.request);
  const ds = await campaignDataset(page.request);
  const judge = await campaignJudge(page.request);
  expect(judge, "n12e_cn_tone judge (spec 41)").toBeTruthy();
  const kimi = (await evaluators(page.request)).find((e) => e.name === "n12e_help_kimi");
  const custom = [judge!.id, ...(kimi ? [kimi.id] : [])];
  ledger(`case26 agent=${agent.name} dataset=${ds.name} (${ds.id}) custom=${custom.join(",")}`);

  // ── Run A: dataset replay (the §E.3 flow) ─────────────────────────────────
  let runA: EvalRun | undefined;
  let secondsA = 0;
  try {
    const runId = await createTaskViaWizard(page, {
      name: e2eName("batch"),
      agentName: agent.name,
      source: "dataset",
      datasetName: ds.name,
      evaluatorIds: [...BUILTINS_A, ...custom],
      evidencePrefix: "case26a",
    });
    const r = await finish(page, testInfo, runId, "case26a");
    runA = r.run;
    secondsA = r.seconds;
  } catch (err) {
    ledger(`case26a dataset replay could not be submitted: ${String((err as Error).message).slice(0, 300)}`);
  }
  if (runA) {
    expect.soft(runA.status, `run A (dataset replay) ended ${runA.status}: ${runA.error ?? ""}`).toBe("completed");
    expect.soft(secondsA, "§4.1: scores within 30 min").toBeLessThanOrEqual(30 * 60);
  } else {
    expect.soft(runA, "run A (dataset replay) was submitted").toBeTruthy();
  }

  // ── Run B: trace window (no replay) ───────────────────────────────────────
  const runIdB = await createTaskViaWizard(page, {
    name: e2eName("window"),
    agentName: agent.name,
    source: "window",
    evaluatorIds: [...BUILTINS_B, ...custom],
    evidencePrefix: "case26b",
  });
  const b = await finish(page, testInfo, runIdB, "case26b");
  expect(b.run.status, `run B (window) ended ${b.run.status}: ${b.run.error ?? ""}`).toBe("completed");
  expect(b.seconds, "§4.1: scores within 30 min").toBeLessThanOrEqual(30 * 60);

  // scores + results of whichever run completed (A preferred)
  const done = runA?.status === "completed" ? runA : b.run;
  const scores = (done.scores ?? []) as Array<Record<string, unknown>>;
  ledger(`case26 scores (${done.id}): ${JSON.stringify(scores).slice(0, 400)}`);
  // GET /runs/{id}/results → {run_id, available, sessions:[{session_id, results:[{evaluator_id, score, label, explanation, error_type, error_message}]}]}
  const results = await (await page.request.get(`/api/eval/runs/${done.id}/results`)).json();
  const sessions = (results.sessions ?? []) as Array<{ session_id: string; results: Array<Record<string, unknown>> }>;
  const rows = sessions.flatMap((s) => s.results.map((r): Record<string, unknown> => ({ ...r, session_id: s.session_id })));
  await record(testInfo, "results.json", results, `${sessions.length} sessions, ${rows.length} result records`);
  const withExplanation = rows.filter((r) => String(r.explanation ?? "").trim().length > 0);
  const errors = rows.filter((r) => r.error_type || r.error_code || r.error_message);
  ledger(`case26 result records=${rows.length} scored=${rows.filter((r) => r.score != null).length} with_explanation=${withExplanation.length} errors=${errors.length} evaluators=${Array.from(new Set(rows.map((r) => r.evaluator_id))).join(",")}`);
  if (errors.length) ledger(`case26 judge errors (first): ${JSON.stringify(errors[0]).slice(0, 400)}`);
  const kimiRows = kimi ? rows.filter((r) => r.evaluator_id === kimi.id) : [];
  if (kimi) ledger(`case26 Kimi K3 derived evaluator records=${kimiRows.length} scored=${kimiRows.filter((r) => r.score != null).length}`);
  expect(rows.length, "per-session result records").toBeGreaterThan(0);
  expect.soft(errors.length, `judge errors in a "completed" run (N12-bug3/bug4): ${String(errors[0]?.error_message ?? "").slice(0, 120)}`).toBe(0);
  expect.soft(scores.length, "scores table populated").toBeGreaterThan(0);
  expect.soft(withExplanation.length, "explanations present").toBeGreaterThan(0);

  // Observability shows one evaluated session
  const sid = String(rows[0].session_id);
  await page.goto(`/v2/observability?tab=sessions&view=session&id=${encodeURIComponent(sid)}`);
  await expect(page.getByRole("heading", { name: new RegExp(sid.slice(0, 12)) })).toBeVisible({ timeout: 60_000 });
  await page.waitForTimeout(8000);
  await snap(page, "case26-observability-evaluated-session", sid.slice(0, 16));

  ledger(`case26 ${runA?.status === "completed" ? "PASS" : "PARTIAL"} runA=${runA?.id ?? "-"}:${runA?.status ?? "not submitted"} (${secondsA}s) runB=${b.run.id}:${b.run.status} (${b.seconds}s) total ${Math.round((Date.now() - t0) / 1000)}s`);
});

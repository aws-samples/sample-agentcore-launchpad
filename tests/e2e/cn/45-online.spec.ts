import { expect, test } from "@playwright/test";
import { chatOnce, e2eName, ledger } from "./helpers";
import { pickOption, pollUntil, record, smokeAgent, snap } from "./n12e";

/**
 * §4.1 case 28 / plan §E.3 flow 6 — Online evaluation.
 * `/v2/eval/online?view=new`: agent, sampling 100 %, session timeout 1 min, two evaluators →
 * `ENABLED`; chat twice with the agent; the detail `results` fills with series / recent rows
 * (expected ≤ 3 min after the sessions end — the hub scheduler ticks every 60 s); PAUSE →
 * RESUME → DELETE; the session detail shows `online_scores`.
 */
test("case 28 — online evaluation: create ENABLED → 2 chats → results → pause/resume → session online_scores → delete", async ({ page }, testInfo) => {
  test.setTimeout(30 * 60 * 1000);
  const t0 = Date.now();
  const agent = await smokeAgent(page.request);
  const desc = e2eName("online");

  await page.goto("/v2/eval/online?view=new");
  await pickOption(page, "v2-online-agent", new RegExp(agent.name));
  await page.getByTestId("v2-online-desc").fill(desc);
  // default picks are GoalSuccessRate + Helpfulness; live traffic has no ground truth → swap GSR for Coherence
  const gsr = page.getByTestId("v2-online-eval-Builtin.GoalSuccessRate");
  if (await gsr.isChecked().catch(() => false)) await gsr.click();
  const coh = page.getByTestId("v2-online-eval-Builtin.Coherence");
  if (!(await coh.isChecked().catch(() => false))) await coh.click();
  await page.getByTestId("v2-online-sampling").fill("100");
  // the second spinbutton is the session idle timeout (minutes)
  await page.getByRole("spinbutton").nth(1).fill("1");
  await snap(page, "case28-online-new-form");
  const before = new Set(((await (await page.request.get("/api/eval/online")).json()).configs as Array<{ config_id: string }>).map((c) => c.config_id));
  await page.getByTestId("v2-online-submit").click();
  await expect(page).not.toHaveURL(/view=new/, { timeout: 60_000 });
  const configs = (await (await page.request.get("/api/eval/online")).json()).configs as Array<Record<string, unknown>>;
  const cfg = configs.find((c) => !before.has(String(c.config_id)));
  expect(cfg, "new online config").toBeTruthy();
  const id = String(cfg!.config_id);
  await record(testInfo, "config.json", cfg, `config ${id} ${cfg!.execution_status} sampling=${cfg!.sampling_percentage} timeout=${cfg!.session_timeout_minutes} evaluators=${JSON.stringify(cfg!.evaluators)}`);
  expect(String(cfg!.execution_status)).toBe("ENABLED");
  expect(Number(cfg!.sampling_percentage)).toBe(100);
  expect(Number(cfg!.session_timeout_minutes)).toBe(1);
  expect((cfg!.evaluators as string[]).length).toBe(2);
  await page.goto(`/v2/eval/online?view=detail&id=${id}`);
  await snap(page, "case28-online-enabled");

  // two live sessions
  const sids: string[] = [];
  for (const prompt of ["What is the magic word?", "Which two AWS regions are in mainland China? One sentence."]) {
    await page.goto(`/v2/chat?agent=${encodeURIComponent(agent.id)}`);
    const r = await chatOnce(page, agent.id, prompt);
    ledger(`case28 chat session=${r.sessionId.slice(0, 12)} ${r.seconds}s errors=${r.errors} "${r.answer.slice(0, 60).replace(/\n/g, " ")}"`);
    expect(r.errors).toBe(0);
    sids.push(r.sessionId);
  }
  const chatsDone = Date.now();

  // results appear once the sessions time out (1 min) and the scheduler tick picks them up
  const { body: results, seconds: waited } = await pollUntil<Record<string, unknown>>(
    page.request,
    `/api/eval/online/${id}/results`,
    (b) => {
      const recent = (b.recent ?? b.results ?? []) as unknown[];
      const samples = (b.samples ?? []) as unknown[];
      return recent.length > 0 || samples.some((s) => (s as { status?: string }).status === "evaluated");
    },
    { timeoutMs: 15 * 60 * 1000, intervalMs: 15_000, label: "case28 results", key: (b) => JSON.stringify({ recent: ((b.recent ?? b.results ?? []) as unknown[]).length, series: ((b.series ?? []) as unknown[]).length, samples: ((b.samples ?? []) as unknown[]).length, stats: b.stats }).slice(0, 200) },
  );
  const sinceChats = Math.round((Date.now() - chatsDone) / 1000);
  await record(testInfo, "results.json", results, `results after ${waited}s (${sinceChats}s after the 2nd chat)`);
  ledger(`case28 first results ${sinceChats}s after the last chat (§E.3 expects ≤ 180 s)`);
  await page.goto(`/v2/eval/online?view=detail&id=${id}`);
  await page.waitForTimeout(3000);
  await snap(page, "case28-online-results", `${sinceChats}s after chats`);

  // PAUSE → RESUME
  const pause = await page.request.post(`/api/eval/online/${id}/pause`);
  const paused = await (await page.request.get(`/api/eval/online/${id}`)).json();
  ledger(`case28 pause → ${pause.status()} execution_status=${paused.execution_status}`);
  expect(String(paused.execution_status)).toBe("DISABLED");
  await page.reload();
  await snap(page, "case28-online-paused");
  const resume = await page.request.post(`/api/eval/online/${id}/resume`);
  const resumed = await (await page.request.get(`/api/eval/online/${id}`)).json();
  ledger(`case28 resume → ${resume.status()} execution_status=${resumed.execution_status}`);
  expect(String(resumed.execution_status)).toBe("ENABLED");

  // session detail carries online_scores
  const sess = await (await page.request.get(`/api/observability/sessions/${sids[0]}?range=24h`)).json();
  const online = sess.online_scores ?? sess.online ?? null;
  await record(testInfo, "session-detail.json", { online_scores: online }, `online_scores on session ${sids[0].slice(0, 12)}: ${JSON.stringify(online)?.slice(0, 200)}`);
  await page.goto(`/v2/observability?tab=sessions&view=session&id=${encodeURIComponent(sids[0])}`);
  await page.waitForTimeout(8000);
  await snap(page, "case28-session-online-scores");
  expect(online, "session detail exposes online_scores").toBeTruthy();

  // DELETE
  const del = await page.request.delete(`/api/eval/online/${id}`);
  ledger(`case28 delete → ${del.status()}`);
  expect(del.ok()).toBeTruthy();
  const gone = await page.request.get(`/api/eval/online/${id}`);
  expect(gone.status()).toBe(404);
  await page.goto("/v2/eval/online");
  await snap(page, "case28-online-deleted");
  ledger(`case28 PASS results after ${sinceChats}s; total ${Math.round((Date.now() - t0) / 1000)}s`);
});

import { expect, test } from "@playwright/test";
import { chatOnce, ledger, sleep } from "../helpers";
import { EVAL_WORKER_PRESET, preset, record, snap, stateRequire, stateSet, stepGate } from "./scn";

/**
 * s06 — observe (plan §3 step 6; REQUIREMENTS §4.1 case 10 + N12-obs31).
 * A fresh turn on the HR assistant becomes visible in Observability within 120 s (Transaction
 * Search lag); the sessions list covers the three storyline agents; the session detail of the
 * A2A turn and of the gateway-tool turn render their traces; the evaluation-worker preset
 * exposes version / release / last job (admin can read the job).
 */
test("s06 — first trace of a new turn ≤ 120 s; sessions ≥ 3 agents; tool / A2A session details render; worker preset observability", async ({ page }, testInfo) => {
  stepGate("s06");
  test.setTimeout(30 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  const sessions = stateRequire("sessions", "s05");

  await page.goto(`/v2/chat?agent=${encodeURIComponent(hr.id)}`);
  const turn = await chatOnce(page, hr.id, "Observability probe: reply with the single word PONG.");
  const t0 = Date.now();
  let lag = -1;
  let traces: Array<Record<string, unknown>> = [];
  let obsSessions: Array<Record<string, unknown>> = [];
  while (Date.now() - t0 < 15 * 60 * 1000) {
    traces = ((await (await page.request.get("/api/observability/traces?range=24h&force=true", { timeout: 60_000 })).json()) as { traces?: Array<Record<string, unknown>> }).traces ?? [];
    obsSessions = ((await (await page.request.get("/api/observability/sessions?range=24h&force=true", { timeout: 60_000 })).json()) as { sessions?: Array<Record<string, unknown>> }).sessions ?? [];
    const hit = traces.some((r) => String(r.session_id) === turn.sessionId) || obsSessions.some((s) => String(s.session_id) === turn.sessionId);
    if (hit) {
      lag = Math.round((Date.now() - t0) / 1000);
      break;
    }
    await sleep(5000);
  }
  await record(testInfo, "traces.json", traces.slice(0, 50), `traces=${traces.length} sessions=${obsSessions.length} lag=${lag}s`);
  const storylineSessions = new Set([...sessions.hr, ...sessions.runtime, turn.sessionId]);
  const seen = obsSessions.filter((s) => storylineSessions.has(String(s.session_id))).length;
  ledger(`s06 storyline sessions visible in Observability: ${seen}/${storylineSessions.size}`);

  await page.goto("/v2/observability?tab=traces&range=24h");
  await page.getByTestId("v2-obs-refresh").click().catch(() => {});
  await expect(page.getByTestId("v2-obs-traces-table")).toBeVisible({ timeout: 60_000 });
  await snap(page, "s06-traces", `${traces.length} traces`);
  await page.goto("/v2/observability?tab=sessions&range=24h");
  await expect(page.getByTestId("v2-obs-sessions-table")).toBeVisible({ timeout: 60_000 });
  await snap(page, "s06-sessions", `${obsSessions.length} sessions`, { key: true });
  // the gateway-tool turn (session 3) and the A2A turn (session 4) of s05a
  for (const [label, sid] of [["tool", sessions.hr[2]], ["a2a", sessions.hr[3]]] as const) {
    expect(sid, `s05a recorded the ${label} session (sessions.hr has ${sessions.hr.length} entries)`).toBeTruthy();
    await page.goto(`/v2/observability?view=session&id=${encodeURIComponent(sid)}&range=24h`);
    await expect(page.getByTestId("v2-obs-session-traces").or(page.getByTestId("v2-obs-session-notfound"))).toBeVisible({ timeout: 60_000 });
    const found = await page.getByTestId("v2-obs-session-traces").isVisible().catch(() => false);
    ledger(`s06 session detail ${label} ${sid.slice(0, 8)} traces visible=${found}`);
    await snap(page, `s06-session-${label}`, sid.slice(0, 12));
    expect(found, `${label} session detail renders its traces`).toBe(true);
  }

  // preset observability (N12-obs31 a+c): version / release / last_job; admin reads the job
  const worker = await preset(page.request, EVAL_WORKER_PRESET);
  await record(testInfo, "eval-worker-preset.json", worker, `version=${worker?.version} release=${worker?.release} installed=${worker?.installed_release}`);
  expect(worker?.release, "preset release recorded").toBeTruthy();
  expect(worker?.installed_release, "installed release recorded").toBeTruthy();
  const lastJob = (worker?.last_job ?? {}) as { id?: string };
  if (lastJob.id) {
    const job = await page.request.get(`/api/system-agents/${EVAL_WORKER_PRESET}/jobs/${lastJob.id}`, { timeout: 60_000 });
    ledger(`s06 admin GET preset job ${lastJob.id.slice(0, 8)} → ${job.status()}`);
    expect(job.status(), "admin reads the preset's last job").toBe(200);
  }

  expect(lag, "new turn's trace/session visible").toBeGreaterThanOrEqual(0);
  expect(lag, "first trace lag ≤ 120 s").toBeLessThanOrEqual(120);
  expect(obsSessions.length, "sessions ≥ the 3 storyline agents").toBeGreaterThanOrEqual(3);
  stateSet("observe", { first_trace_lag_s: lag });
  stateSet("sessions", { ...sessions, hr: [...sessions.hr, turn.sessionId] });
  ledger(`s06 PASS lag=${lag}s traces=${traces.length} sessions=${obsSessions.length}`);
});

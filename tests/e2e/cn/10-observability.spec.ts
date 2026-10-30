import { expect, test } from "@playwright/test";
import { activeAgent, attach, chatOnce, evidence, isE2E, ledger, listAgents, sleep } from "./helpers";

/**
 * Case 10 — Observability: sessions ≥ number of deployed e2e agents; a fresh chat turn's
 * trace becomes visible within 120 s (Transaction Search lag; §4.1 amended 2026-10-10 from 60 s).
 */
test("case 10: sessions ≥ deployed agents; first trace of a new turn visible ≤ 120 s", async ({ page }, testInfo) => {
  ledger("case10 start");
  const agents = (await listAgents(page.request)).filter((a) => isE2E(a.name) && a.status === "active");
  const names = new Set(agents.map((a) => String(a.name)));
  const a = await activeAgent(page.request, "harness");
  expect(a, "case 2 agent required").toBeTruthy();

  // a brand-new turn → measure the lag until its session/trace is listed
  const turn = await chatOnce(page, a!.id, "Observability probe: reply with the single word PONG.");
  const t0 = Date.now();
  let lag = -1;
  let traces: Array<Record<string, unknown>> = [];
  let sessions: Array<Record<string, unknown>> = [];
  while (Date.now() - t0 < 15 * 60 * 1000) {
    const tr = await (await page.request.get("/api/observability/traces?range=24h&force=true")).json();
    const se = await (await page.request.get("/api/observability/sessions?range=24h&force=true")).json();
    traces = tr.traces ?? [];
    sessions = se.sessions ?? [];
    const hit = traces.some((r) => String(r.session_id) === turn.sessionId) || sessions.some((s) => String(s.session_id) === turn.sessionId);
    if (hit) { lag = Math.round((Date.now() - t0) / 1000); break; }
    await sleep(5000);
  }
  const mine = traces.filter((r) => names.has(String(r.agent)) || names.has(String(r.service)));
  ledger(`case10 traces=${traces.length} (e2e ${mine.length}) sessions=${sessions.length} lag=${lag}s agents=${agents.length}`);
  await attach(testInfo, "traces.json", traces.slice(0, 50));
  await attach(testInfo, "sessions.json", sessions.slice(0, 50));

  await page.goto("/v2/observability?tab=traces&range=24h");
  await page.getByTestId("v2-obs-refresh").click().catch(() => {});
  await expect(page.getByTestId("v2-obs-traces-table")).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case10-observability-traces");
  await page.goto("/v2/observability?tab=sessions&range=24h");
  await expect(page.getByTestId("v2-obs-sessions-table")).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case10-observability-sessions");
  await page.goto(`/v2/observability?view=session&id=${encodeURIComponent(turn.sessionId)}&range=24h`);
  await expect(page.getByTestId("v2-obs-session-traces").or(page.getByTestId("v2-obs-session-notfound"))).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case10-observability-session-detail");

  expect(lag, "new turn's trace/session visible").toBeGreaterThanOrEqual(0);
  expect(lag, "first trace lag ≤ 120 s").toBeLessThanOrEqual(120);
  expect(sessions.length, "sessions ≥ deployed e2e agents").toBeGreaterThanOrEqual(agents.length);
  ledger(`case10 end PASS traces=${traces.length} sessions=${sessions.length} lag=${lag}s`);
});

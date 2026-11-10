import { expect, test } from "@playwright/test";
import { ledger } from "./helpers";
import { chatSessions, record, smokeAgent, snap } from "./n12e";

/**
 * §4.1 case 27 / plan §E.3 flow 4 — SCORE NOW (on-demand scoring, in-process, no worker job).
 * Observability → session detail: `v2-obs-score-now` card → pick ≤ 5 evaluators
 * (`v2-obs-score-<id>` checkboxes) → `v2-obs-score-run` → confirm → `v2-obs-score-results`
 * renders scores without a run. The same request is cross-checked through
 * `POST /api/observability/sessions/{id}/evaluate` (judge = GLM 5.3 via Global Bedrock).
 */
const PICK = ["Builtin.Helpfulness", "Builtin.Coherence"];

test("case 27 — SCORE NOW on a chat session renders scores without a run", async ({ page }, testInfo) => {
  test.setTimeout(20 * 60 * 1000);
  const t0 = Date.now();
  const agent = await smokeAgent(page.request);
  const sessions = await chatSessions(page.request, agent.id);
  expect(sessions.length, "the smoke agent has chat sessions (spec 40)").toBeGreaterThanOrEqual(1);
  // the oldest session has had the most time to be indexed in CloudWatch
  const sid = sessions[sessions.length - 1].session_id;
  ledger(`case27 agent=${agent.name} session=${sid}`);

  await page.goto(`/v2/observability?tab=sessions&view=session&id=${encodeURIComponent(sid)}`);
  const card = page.getByTestId("v2-obs-score-now");
  await expect(card, "SCORE NOW card on the session detail").toBeVisible({ timeout: 120_000 });
  await snap(page, "case27-session-detail", "SCORE NOW card visible");
  for (const id of PICK) {
    const box = card.getByTestId(`v2-obs-score-${id}`);
    await expect(box, `${id} offered in the picker`).toBeVisible();
    await box.click();
  }
  // a ground-truth evaluator must be blocked for on-demand scoring (no dataset)
  const gsr = card.getByTestId("v2-obs-score-Builtin.GoalSuccessRate");
  const gsrBlocked = (await gsr.isDisabled().catch(() => false)) || (await gsr.getAttribute("aria-disabled")) === "true";
  ledger(`case27 GoalSuccessRate blocked in picker (needs ground truth) = ${gsrBlocked}`);
  await snap(page, "case27-picker", PICK.join(","));

  const tClick = Date.now();
  await card.getByTestId("v2-obs-score-run").click();
  const ok = page.getByTestId("v2-confirm-ok");
  if (await ok.isVisible({ timeout: 3000 }).catch(() => false)) await ok.click();
  const results = card.getByTestId("v2-obs-score-results");
  const error = card.getByTestId("v2-obs-score-error");
  await expect(results.or(error).first()).toBeVisible({ timeout: 10 * 60 * 1000 });
  const uiSeconds = Math.round((Date.now() - tClick) / 1000);
  if (await error.isVisible().catch(() => false)) {
    const msg = (await error.innerText()).trim();
    ledger(`case27 UI error after ${uiSeconds}s: ${msg.slice(0, 300)}`);
    await snap(page, "case27-score-error", msg.slice(0, 80));
  }
  await expect(results, "scores rendered in the SCORE NOW card").toBeVisible();
  const rowsText = (await results.locator("tbody tr").allInnerTexts()).map((s) => s.replace(/\s+/g, " ").trim());
  ledger(`case27 UI results after ${uiSeconds}s: ${rowsText.join(" || ").slice(0, 500)}`);
  await snap(page, "case27-score-results", `${rowsText.length} rows in ${uiSeconds}s`);
  expect(rowsText.length).toBeGreaterThanOrEqual(PICK.length);

  // cross-check through the API (same engine path, persist=False)
  const tApi = Date.now();
  const res = await page.request.post(`/api/observability/sessions/${sid}/evaluate`, { data: { evaluator_ids: PICK, range: "24h" }, timeout: 10 * 60 * 1000 });
  const body = await res.json().catch(async () => ({ raw: await res.text() }));
  const apiSeconds = Math.round((Date.now() - tApi) / 1000);
  await record(testInfo, "evaluate-api.json", body, `API ${res.status()} in ${apiSeconds}s span_count=${body.span_count} results=${body.results?.length}`);
  expect(res.ok(), JSON.stringify(body).slice(0, 300)).toBeTruthy();
  const apiRows = (body.results ?? []) as Array<{ evaluator_id: string; value?: number; label?: string; explanation?: string; error_code?: string }>;
  expect(apiRows.length).toBe(PICK.length);
  const scored = apiRows.filter((r) => r.error_code == null && r.value != null);
  ledger(`case27 API rows: ${apiRows.map((r) => `${r.evaluator_id}=${r.value ?? r.error_code} (${r.label ?? ""})`).join("; ")}`);
  expect(scored.length, "every picked evaluator returned a score").toBe(PICK.length);
  // no eval_results row may be persisted for an on-demand score (CA-8: persist=False)
  const runs = await (await page.request.get("/api/eval/runs")).json();
  expect((runs.runs as Array<{ dataset_name?: string }>).some((r) => String(r.dataset_name ?? "").startsWith("ondemand")), "no run row created").toBeFalsy();

  // > 5 evaluators must be refused (422 observability.too_many_evaluators)
  const tooMany = await page.request.post(`/api/observability/sessions/${sid}/evaluate`, {
    data: { evaluator_ids: ["Builtin.Helpfulness", "Builtin.Coherence", "Builtin.Conciseness", "Builtin.Refusal", "Builtin.Harmfulness", "Builtin.Stereotyping"], range: "24h" },
  });
  ledger(`case27 6 evaluators → ${tooMany.status()} ${(await tooMany.text()).slice(0, 120)}`);
  expect(tooMany.status()).toBe(422);

  ledger(`case27 PASS ui=${uiSeconds}s api=${apiSeconds}s total ${Math.round((Date.now() - t0) / 1000)}s`);
});

import { expect, test } from "@playwright/test";
import { chatOnce, ledger, UPSTREAM_TRANSIENT_RE } from "../helpers";
import { createTaskViaWizard } from "../n12e";
import { softly, datasets, evaluators, JUDGE_ALT, JUDGE_DEFAULT, pickOption, pollUntil, record, SCN_EVALUATOR_PREFIX, scnName, snap, stateRequire, stateSet, stepGate, waitForRun } from "./scn";

/**
 * s07 — evaluate (plan §3 step 7; REQUIREMENTS §4.1 cases 24–29).
 *
 * The HR assistant's own chat sessions (s05/s06) become the dataset: evaluators (GLM judge
 * `e2es_hr_tone`, Kimi-K3 derived Helpfulness, deterministic builtins) → dataset FROM SESSIONS
 * → ground truth → SYNC → PUBLISH VERSION → batch replay runs on the evaluation worker for both
 * the HR assistant and the runtime twin (≤ 30 min, scores + explanations) → SCORE NOW on a
 * session → online evaluation while chatting → insights run. The completed runs feed s08.
 */
test.describe.configure({ mode: "serial" });

const JUDGE = `${SCN_EVALUATOR_PREFIX}hr_tone`;
const KIMI = `${SCN_EVALUATOR_PREFIX}help_kimi`;
const RUBRIC =
  "Judge whether {assistant_turn} answers the employee's HR request in {context} politely, concisely, in English, and " +
  "grounded in Octank policy or tool results rather than guesses. A grounded, polite, concise reply passes; a rude, " +
  "rambling, wrong-language or unsupported reply fails.";

test("s07a — evaluators: GLM judge via the V2 form (picker = whole catalog, unconfigured providers greyed), Kimi-K3 derived Helpfulness via the API", async ({ page }, testInfo) => {
  stepGate("s07");
  for (const name of [JUDGE, KIMI]) {
    const stale = (await evaluators(page.request)).find((e) => e.name === name);
    if (stale) ledger(`s07a stale ${name} (${stale.id}) → DELETE ${(await page.request.delete(`/api/eval/evaluators/${stale.id}`)).status()}`);
  }
  await page.goto("/v2/eval/evaluators?view=new");
  await expect(page.getByTestId("v2-evaluator-def-judge")).toBeVisible();
  await page.getByTestId("v2-evaluator-name").fill(JUDGE);
  const modelTrigger = page.getByRole("button", { name: JUDGE_DEFAULT });
  await expect(modelTrigger, "GLM 5.3 is the default judge model").toBeVisible();
  await modelTrigger.click();
  // N-R6: the picker lists the whole model catalog served by /api/capabilities — rows of an
  // unconfigured provider (e.g. SiliconFlow without its SSM key) are present but disabled.
  const caps = await (await page.request.get("/api/capabilities", { timeout: 60_000 })).json() as {
    models?: Array<{ model_id: string; available?: boolean }>;
  };
  const catalog = caps.models ?? [];
  const availableIds = catalog.filter((m) => m.available !== false).map((m) => m.model_id);
  const unavailableIds = catalog.filter((m) => m.available === false).map((m) => m.model_id);
  const options = page.getByRole("listbox").getByRole("option");
  const labels = (await options.allInnerTexts()).map((s) => s.trim());
  const enabled = (await options.evaluateAll((els) => els.filter((e) => e.getAttribute("aria-disabled") !== "true").map((e) => e.textContent?.trim() ?? "")));
  const disabled = (await options.evaluateAll((els) => els.filter((e) => e.getAttribute("aria-disabled") === "true").map((e) => e.textContent?.trim() ?? "")));
  expect(labels.length, `picker = whole catalog (${catalog.length} rows)`).toBe(catalog.length);
  expect(enabled.length, `enabled = available models ${availableIds.join(",")}`).toBe(availableIds.length);
  for (const id of availableIds) expect(enabled.some((l) => l.includes(id)), `${id} selectable`).toBeTruthy();
  for (const id of unavailableIds) expect(disabled.some((l) => l.includes(id)), `${id} greyed (provider unconfigured)`).toBeTruthy();
  expect(enabled.some((l) => l.includes(JUDGE_ALT)), `${JUDGE_ALT} selectable`).toBeTruthy();
  expect(enabled.some((l) => l.includes(JUDGE_DEFAULT)), `${JUDGE_DEFAULT} selectable`).toBeTruthy();
  ledger(`s07a judge picker: ${labels.length} rows, ${enabled.length} enabled, ${disabled.length} greyed`);
  await page.getByRole("listbox").getByRole("option").filter({ hasText: JUDGE_DEFAULT }).first().click();
  await page.getByTestId("v2-evaluator-rubric").fill(RUBRIC);
  await snap(page, "s07a-judge-form", labels.join(" | "));
  await page.getByTestId("v2-evaluator-save").click();
  await expect(page).not.toHaveURL(/view=new/, { timeout: 30_000 });
  const judge = (await evaluators(page.request)).find((e) => e.name === JUDGE);
  expect(judge, `${JUDGE} listed`).toBeTruthy();
  expect(judge!.model_id ?? JUDGE_DEFAULT).toBe(JUDGE_DEFAULT);

  const derived = await page.request.post("/api/eval/evaluators", {
    data: { name: KIMI, base_evaluator_id: "Builtin.Helpfulness", model_id: JUDGE_ALT, level: "TRACE", description: "Helpfulness judged by Kimi K3 (e2e scenario)" },
  });
  expect(derived.status(), await derived.text()).toBe(201);
  const kimi = (await evaluators(page.request)).find((e) => e.name === KIMI);
  expect(kimi, `${KIMI} listed`).toBeTruthy();
  await page.goto("/v2/eval/evaluators");
  await page.getByRole("textbox", { name: /Search evaluator/i }).fill(SCN_EVALUATOR_PREFIX);
  await expect(page.getByTestId("v2-evaluators-table").locator("tr", { hasText: JUDGE })).toBeVisible();
  await snap(page, "s07a-evaluators-list");
  await record(testInfo, "evaluators.json", { judge, kimi });
  stateSet("evaluators", { judge: judge!.id, kimi: kimi!.id });
  ledger(`s07a PASS judge=${judge!.id} kimi=${kimi!.id}`);
});

test("s07b — dataset FROM the HR assistant's chat sessions → ground truth → SYNC (snapshot ACTIVE) → PUBLISH VERSION 1 → cloud listing", async ({ page }, testInfo) => {
  stepGate("s07");
  const sessions = stateRequire("sessions", "s05");
  const name = scnName("hr-ds");
  // from-sessions reads every session's spans from CloudWatch (≈ 22 s for 9 sessions in run #4, > 30 s in run #6
  // with ~400 sessions in the 24 h window — N-SCN-obs10): give it the plan's 5-min ceiling, not the 30 s request
  // default. The storyline is 9 sessions — one under FROM_SESSIONS_SYNC_MAX (10) — so `?sync=true` pins the 201
  // shape this step asserts instead of relying on the default's cap (r8-a2 review note A).
  const tFrom = Date.now();
  const res = await page.request.post("/api/eval/datasets/from-sessions?sync=true", {
    data: { session_ids: sessions.hr, range: "24h", name, description: "e2e scenario — the HR assistant's own s05/s06 sessions", first_turn_only: true, dedupe: true },
    timeout: 5 * 60 * 1000,
  });
  ledger(`s07b from-sessions answered ${res.status()} in ${Math.round((Date.now() - tFrom) / 1000)}s for ${sessions.hr.length} sessions`);
  const body = (await res.json().catch(async () => ({ raw: await res.text() }))) as { dataset?: { id: string; items?: Array<Record<string, unknown>> }; added?: number; skipped?: unknown };
  await record(testInfo, "from-sessions.json", body, `→ ${res.status()} added=${body.added} skipped=${JSON.stringify(body.skipped).slice(0, 120)}`);
  expect(res.status(), JSON.stringify(body).slice(0, 300)).toBe(201);
  const id = body.dataset!.id;
  expect(Number(body.added), "≥ 5 of the storyline sessions became scenarios").toBeGreaterThanOrEqual(5);

  // ground truth: an assertion per item (Builtin.GoalSuccessRate needs it on every item it scores)
  let row = (await datasets(page.request)).find((d) => d.id === id)!;
  const items = (row.items ?? []).map((it) => {
    const input = JSON.stringify(it).toLowerCase();
    const assertion = /emp-1024/.test(input)
      ? "The reply names Maya Chen and the HR Operations department."
      : /expense claim/.test(input)
        ? "The reply states the 45-day filing window and that the line manager approves."
        : /hotline/.test(input)
          ? "The reply contains the code OCTANK-HR-0426."
          : "The reply addresses the employee's request about Octank HR matters in English without inventing policy.";
    return { ...it, assertions: [assertion] };
  });
  const put = await page.request.put(`/api/eval/datasets/${id}`, { data: { items } });
  expect(put.ok(), await put.text()).toBeTruthy();
  row = (await datasets(page.request)).find((d) => d.id === id)!;
  await page.goto("/v2/eval/data?tab=datasets");
  await page.getByTestId("v2-tab-datasets").click().catch(() => {});
  await expect(page.getByText(name).first()).toBeVisible({ timeout: 30_000 });
  await snap(page, "s07b-datasets-list", `${items.length} items`);

  const sync = await page.request.post(`/api/eval/datasets/${id}/sync-to-aws`);
  const syncBody = (await sync.json().catch(() => ({}))) as { cloud?: { dataset_id?: string; status?: string; arn?: string } };
  expect(sync.ok(), JSON.stringify(syncBody).slice(0, 300)).toBeTruthy();
  const cloud = syncBody.cloud ?? {};
  expect(String(cloud.status).toUpperCase(), "snapshot ACTIVE").toBe("ACTIVE");
  expect(String(cloud.arn ?? ""), "snapshot in the CN-partition artifacts bucket").toMatch(/^arn:aws-cn:s3:::launchpad-artifacts-/);
  const pub = await page.request.post(`/api/eval/datasets/${id}/publish-version`);
  const pubBody = (await pub.json().catch(() => ({}))) as { cloud?: { versions?: Array<{ version: string; example_count: number }> } };
  expect(pub.ok(), JSON.stringify(pubBody).slice(0, 300)).toBeTruthy();
  const versions = pubBody.cloud?.versions ?? [];
  expect(versions.map((v) => v.version), "first published version is 1").toEqual(["1"]);
  expect(versions[0].example_count).toBe(items.length);
  const cloudList = (await (await page.request.get("/api/eval/datasets/cloud", { timeout: 60_000 })).json()) as { datasets: Array<Record<string, unknown>> };
  expect(cloudList.datasets.some((d) => String(d.datasetId ?? d.id) === String(cloud.dataset_id)), "cloud snapshot listed").toBe(true);
  await record(testInfo, "dataset.json", { row, cloud, versions }, `dataset ${id} items=${items.length} cloud=${cloud.dataset_id} versions=${JSON.stringify(versions)}`);
  stateSet("dataset", { id, name, items: items.length, cloud_id: String(cloud.dataset_id ?? "") });
  ledger(`s07b PASS dataset=${id} items=${items.length}`);
});

test("s07c — batch replay runs on the evaluation worker (HR assistant: GoalSuccessRate + Helpfulness + GLM judge + Kimi judge; runtime twin: Helpfulness + Coherence + GLM judge) → completed ≤ 30 min with scores", async ({ page }, testInfo) => {
  stepGate("s07");
  test.setTimeout(80 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  const runtime = stateRequire("runtime", "s04");
  const ds = stateRequire("dataset", "s07");
  const ev = stateRequire("evaluators", "s07");

  const hrRun = await createTaskViaWizard(page, { name: scnName("batch-hr"), agentName: hr.name, source: "dataset", datasetName: ds.name, evaluatorIds: ["Builtin.GoalSuccessRate", "Builtin.Helpfulness", ev.judge, ev.kimi], evidencePrefix: "s07c-hr" });
  const rtRun = await createTaskViaWizard(page, { name: scnName("batch-rt"), agentName: runtime.name, source: "dataset", datasetName: ds.name, evaluatorIds: ["Builtin.Helpfulness", "Builtin.Coherence", ev.judge], evidencePrefix: "s07c-rt" });
  stateSet("runs", { hr_batch: hrRun, runtime_batch: rtRun });
  const [a, b] = await Promise.all([waitForRun(page.request, hrRun, "s07c hr", 35 * 60 * 1000), waitForRun(page.request, rtRun, "s07c rt", 35 * 60 * 1000)]);

  for (const [label, r] of [["hr", a], ["rt", b]] as const) {
    await record(testInfo, `${label}-run-final.json`, r.run, `${r.run.status} after ${r.seconds}s`);
    await page.goto(`/v2/eval/tasks?view=detail&id=${r.run.id}`);
    await page.waitForTimeout(2500);
    await snap(page, `s07c-${label}-task-final`, `${r.run.status} ${r.seconds}s`);
    expect(r.run.status, `${label} run ended ${r.run.status}: ${r.run.error ?? ""}`).toBe("completed");
    expect(r.seconds, `${label}: scores within 30 min`).toBeLessThanOrEqual(30 * 60);
    const results = (await (await page.request.get(`/api/eval/runs/${r.run.id}/results`, { timeout: 60_000 })).json()) as { sessions?: Array<{ session_id: string; results: Array<Record<string, unknown>> }> };
    const rows = (results.sessions ?? []).flatMap((s) => s.results.map((x): Record<string, unknown> => ({ ...x, session_id: s.session_id })));
    const errors = rows.filter((x) => x.error_type || x.error_code || x.error_message);
    const explained = rows.filter((x) => String(x.explanation ?? "").trim().length > 0);
    ledger(`s07c ${label} records=${rows.length} scored=${rows.filter((x) => x.score != null).length} explained=${explained.length} errors=${errors.length} evaluators=${Array.from(new Set(rows.map((x) => x.evaluator_id))).join(",")}`);
    await record(testInfo, `${label}-results.json`, results);
    expect(rows.length, `${label}: per-session result records`).toBeGreaterThan(0);
    expect(((r.run.scores ?? []) as unknown[]).length, `${label}: scores table populated`).toBeGreaterThan(0);
    expect(explained.length, `${label}: explanations present`).toBeGreaterThan(0);
    // N-SCN-obs21 (lead rulings 2026-10-16): errors in a COMPLETED run are a soft miss only when every errored record is an
    // upstream transient (Bedrock internalServerException / ServiceUnavailable / throttling — the obs18 marker set) AND
    // (errored sessions ≤ 1 OR errored records ≤ 10 %) AND (every evaluator scored all-but-one session OR ≥ 90 % of its
    // records) — the session-based halves keep one transient on a ≤ 10-item dataset soft; anything else is hard.
    const evaluatorIds = Array.from(new Set(rows.map((x) => String(x.evaluator_id))));
    const scoredShare = (ev: string): number => { const mine = rows.filter((x) => String(x.evaluator_id) === ev); return mine.filter((x) => x.score != null).length / Math.max(1, mine.length); };
    const transientOnly = errors.length > 0 && errors.every((x) => UPSTREAM_TRANSIENT_RE.test(`${String(x.error_type ?? "")} ${String(x.error_message ?? "")}`));
    const missedSessions = (ev: string): number => rows.filter((x) => String(x.evaluator_id) === ev && x.score == null).length;
    const erroredSessions = new Set(errors.map((x) => String(x.session_id))).size;
    const withinBounds = transientOnly
      && (erroredSessions <= 1 || errors.length / Math.max(1, rows.length) <= 0.1)
      && evaluatorIds.every((ev) => missedSessions(ev) <= 1 || scoredShare(ev) >= 0.9);
    if (withinBounds) {
      const shares = evaluatorIds.map((ev) => `${ev} ${Math.round(scoredShare(ev) * 100)} %`).join(", ");
      testInfo.annotations.push({ type: "observation", description: `N-SCN-obs21: ${label} run completed with ${errors.length}/${rows.length} upstream-transient error record(s) in ${erroredSessions} session(s) — ${String(errors[0]?.error_message ?? "").slice(0, 100)}; scored ${shares} — soft` });
      ledger(`s07c ${label} N-SCN-obs21 soft: ${errors.length}/${rows.length} upstream-transient error record(s) in ${erroredSessions} session(s); scored ${shares}`);
      softly(testInfo, `${label}: judge errors in a completed run (upstream transient, within the N-SCN-obs21 bounds)`, () => expect(errors.length).toBe(0));
    } else {
      expect(errors.length, `${label}: judge errors in a completed run: ${String(errors[0]?.error_message ?? "").slice(0, 120)}`).toBe(0);
    }
  }
  ledger(`s07c PASS hr=${hrRun} (${a.seconds}s) rt=${rtRun} (${b.seconds}s)`);
});

test("s07d — SCORE NOW on the KB session (UI card + API): 2 evaluators scored without a run; 6 evaluators → 422", async ({ page }, testInfo) => {
  stepGate("s07");
  test.setTimeout(25 * 60 * 1000);
  const sessions = stateRequire("sessions", "s05");
  const sid = sessions.hr[1]; // the kb_search session of s05a
  const PICK = ["Builtin.Helpfulness", "Builtin.Coherence"];
  await page.goto(`/v2/observability?tab=sessions&view=session&id=${encodeURIComponent(sid)}`);
  const card = page.getByTestId("v2-obs-score-now");
  await expect(card, "SCORE NOW card").toBeVisible({ timeout: 120_000 });
  for (const id of PICK) await card.getByTestId(`v2-obs-score-${id}`).click();
  const tClick = Date.now();
  await card.getByTestId("v2-obs-score-run").click();
  const ok = page.getByTestId("v2-confirm-ok");
  if (await ok.isVisible({ timeout: 3000 }).catch(() => false)) await ok.click();
  const results = card.getByTestId("v2-obs-score-results");
  await expect(results.or(card.getByTestId("v2-obs-score-error")).first()).toBeVisible({ timeout: 10 * 60 * 1000 });
  await expect(results, "scores rendered without a run").toBeVisible();
  const rowsText = (await results.locator("tbody tr").allInnerTexts()).map((s) => s.replace(/\s+/g, " ").trim());
  await snap(page, "s07d-score-now", `${rowsText.length} rows in ${Math.round((Date.now() - tClick) / 1000)}s`, { key: true });
  expect(rowsText.length).toBeGreaterThanOrEqual(PICK.length);
  const api = await page.request.post(`/api/observability/sessions/${sid}/evaluate`, { data: { evaluator_ids: PICK, range: "24h" }, timeout: 10 * 60 * 1000 });
  const body = (await api.json().catch(async () => ({ raw: await api.text() }))) as { results?: Array<{ evaluator_id: string; value?: number; error_code?: string }> };
  await record(testInfo, "score-now-api.json", body, `API ${api.status()} results=${body.results?.length}`);
  expect(api.ok()).toBeTruthy();
  expect((body.results ?? []).filter((r) => r.error_code == null && r.value != null).length, "every picked evaluator scored").toBe(PICK.length);
  const tooMany = await page.request.post(`/api/observability/sessions/${sid}/evaluate`, { data: { evaluator_ids: ["Builtin.Helpfulness", "Builtin.Coherence", "Builtin.Conciseness", "Builtin.Refusal", "Builtin.Harmfulness", "Builtin.Stereotyping"], range: "24h" } });
  expect(tooMany.status(), "> 5 evaluators refused").toBe(422);
  ledger("s07d PASS");
});

test("s07e — online evaluation while chatting: ENABLED (100 %, 1 min, 2 evaluators) → 2 chats → results ≤ 3 min → PAUSE (left DISABLED for the cleanup)", async ({ page }, testInfo) => {
  stepGate("s07");
  test.setTimeout(30 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  await page.goto("/v2/eval/online?view=new");
  await pickOption(page, "v2-online-agent", new RegExp(hr.name));
  await page.getByTestId("v2-online-desc").fill(scnName("online"));
  const gsr = page.getByTestId("v2-online-eval-Builtin.GoalSuccessRate");
  if (await gsr.isChecked().catch(() => false)) await gsr.click();
  const coh = page.getByTestId("v2-online-eval-Builtin.Coherence");
  if (!(await coh.isChecked().catch(() => false))) await coh.click();
  await page.getByTestId("v2-online-sampling").fill("100");
  await page.getByRole("spinbutton").nth(1).fill("1");
  const before = new Set((((await (await page.request.get("/api/eval/online", { timeout: 60_000 })).json()) as { configs: Array<{ config_id: string }> }).configs).map((c) => c.config_id));
  await page.getByTestId("v2-online-submit").click();
  await expect(page).not.toHaveURL(/view=new/, { timeout: 60_000 });
  const configs = ((await (await page.request.get("/api/eval/online", { timeout: 60_000 })).json()) as { configs: Array<Record<string, unknown>> }).configs;
  const cfg = configs.find((c) => !before.has(String(c.config_id)))!;
  expect(cfg, "new online config").toBeTruthy();
  const id = String(cfg.config_id);
  expect(String(cfg.execution_status)).toBe("ENABLED");
  stateSet("online", { config_id: id });

  for (const prompt of ["How many days of annual leave does an employee with 7 years of service get?", "When is salary paid at Octank?"]) {
    await page.goto(`/v2/chat?agent=${encodeURIComponent(hr.id)}`);
    const r = await chatOnce(page, hr.id, prompt);
    expect(r.errors).toBe(0);
  }
  const chatsDone = Date.now();
  const { body: results } = await pollUntil<Record<string, unknown>>(
    page.request,
    `/api/eval/online/${id}/results`,
    (b) => ((b.recent ?? b.results ?? []) as unknown[]).length > 0 || ((b.samples ?? []) as Array<{ status?: string }>).some((s) => s.status === "evaluated"),
    { timeoutMs: 15 * 60 * 1000, intervalMs: 15_000, label: "s07e results", key: (b) => JSON.stringify({ recent: ((b.recent ?? b.results ?? []) as unknown[]).length, samples: ((b.samples ?? []) as unknown[]).length }) },
  );
  const sinceChats = Math.round((Date.now() - chatsDone) / 1000);
  await record(testInfo, "online-results.json", results, `first results ${sinceChats}s after the 2nd chat`);
  await page.goto(`/v2/eval/online?view=detail&id=${id}`);
  await page.waitForTimeout(3000);
  await snap(page, "s07e-online-results", `${sinceChats}s`, { key: true });
  softly(testInfo, "results ≤ 180 s after the sessions end", () => expect(sinceChats, "results ≤ 180 s after the sessions end").toBeLessThanOrEqual(180));
  const pause = await page.request.post(`/api/eval/online/${id}/pause`);
  expect(pause.ok()).toBeTruthy();
  const paused = (await (await page.request.get(`/api/eval/online/${id}`, { timeout: 60_000 })).json()) as { execution_status?: string };
  expect(String(paused.execution_status)).toBe("DISABLED");
  ledger(`s07e PASS config=${id} results after ${sinceChats}s`);
});

test("s07f — insights run on the dataset (HR assistant) → completed ≤ 30 min with clusters on /v2/eval/insights", async ({ page }, testInfo) => {
  stepGate("s07");
  test.setTimeout(50 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  const ds = stateRequire("dataset", "s07");
  const id = await createTaskViaWizard(page, { name: scnName("insights"), agentName: hr.name, mode: "insights", source: "dataset", datasetName: ds.name, evidencePrefix: "s07f" });
  const { run, seconds } = await waitForRun(page.request, id, "s07f insights", 35 * 60 * 1000);
  await record(testInfo, "insights-run.json", run, `${run.status} after ${seconds}s`);
  await page.goto(`/v2/eval/tasks?view=detail&id=${id}`);
  await page.waitForTimeout(2500);
  await snap(page, "s07f-insights-task", run.status);
  expect(run.status, `insights run ended ${run.status}: ${run.error ?? ""}`).toBe("completed");
  expect(seconds).toBeLessThanOrEqual(30 * 60);
  expect(Object.keys((run.insights ?? {}) as object).length, "run.insights populated").toBeGreaterThan(0);
  await page.goto("/v2/eval/insights");
  await expect(page.getByTestId("v2-insights-panel")).toBeVisible({ timeout: 60_000 });
  await page.waitForTimeout(3000);
  const panel = (await page.getByTestId("v2-insights-panel").innerText()).replace(/\s+/g, " ");
  await snap(page, "s07f-insights-overview", panel.slice(0, 80), { key: true });
  expect(panel).not.toMatch(/No completed insights task/i);
  const runs = stateRequire("runs", "s07");
  stateSet("runs", { ...runs, insights: id });
  ledger(`s07f PASS insights=${id} ${seconds}s`);
});

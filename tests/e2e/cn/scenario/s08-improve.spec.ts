import { expect, test, type APIRequestContext } from "@playwright/test";
import { awsRead, chatOnce, getAgent, ledger, waitForJob } from "../helpers";
import { createCanaryViaUi, expectConfigurationRedirect } from "../canary-ui";
import { softly, act, gotoRendered, pollUntil, record, runtimeEndpoints, settle, snap, stateGet, stateRequire, stateSet, stepGate, TOKENS } from "./scn";

/**
 * s08 — improve (plan §3 step 8; REQUIREMENTS §4.1 cases 30 + 31).
 *
 *   a) HR assistant: RECOMMEND (gepa_lite / GLM) on the completed s07 batch run → COMPLETED
 *      with prompt + explanation → ACCEPT → redeploy job → version +1, prompt replaced.
 *   b) Configuration experiment — retired (N13-obs2, user decision 2026-10-17): only the
 *      canary-list redirect of its deep links and the 404 of `/api/experiments` are asserted.
 *   c) Runtime twin — Runtime canary: candidate prompt → setup (`stable…` / `treat…` endpoints,
 *      read-only `list-agent-runtime-endpoints`) → traffic → verdict → advance 50/50 → advance →
 *      complete (rollback as the recorded fallback) → cleanup removes the endpoints.
 */
test.describe.configure({ mode: "serial" });

test("s08a — recommendation on the HR assistant's batch run → COMPLETED → ACCEPT → redeploy succeeds, version +1, prompt replaced", async ({ page }, testInfo) => {
  stepGate("s08");
  test.setTimeout(60 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  const runs = stateRequire("runs", "s07");
  const runId = runs.hr_batch!;
  const api = page.request;
  const before = await getAgent(api, hr.id);
  const promptBefore = String((before.spec as Record<string, unknown>).system_prompt ?? "");

  await page.goto(`/v2/eval/tasks?view=detail&id=${runId}`);
  const form = page.getByTestId("v2-rec-form");
  const newBtn = page.getByTestId("v2-rec-new");
  await expect(form.or(newBtn).first(), "RECOMMEND available on the completed run").toBeVisible({ timeout: 60_000 });
  if (!(await form.isVisible().catch(() => false))) await newBtn.click();
  await expect(form).toBeVisible();
  const providerTrigger = page.getByTestId("v2-rec-provider");
  if (await providerTrigger.isVisible().catch(() => false)) {
    await providerTrigger.click();
    await page.getByRole("listbox").getByRole("option", { name: /GEPA/i }).first().click();
  }
  const sp = page.getByTestId("v2-rec-kind-sp");
  if (!(await sp.isChecked().catch(() => false))) await sp.click().catch(() => {});
  const promptBox = page.getByTestId("v2-rec-prompt");
  if ((await promptBox.isVisible().catch(() => false)) && !(await promptBox.inputValue()).trim()) await promptBox.fill(promptBefore);
  await snap(page, "s08a-recommend-form");
  const beforeRecs = (((await (await api.get(`/api/eval/runs/${runId}/recommendations`, { timeout: 60_000 })).json()) as { recommendations?: Array<{ id: string }> }).recommendations ?? []);
  await page.getByTestId("v2-rec-submit").click();
  const { body: list, seconds } = await pollUntil<{ recommendations: Array<Record<string, unknown>> }>(
    api,
    `/api/eval/runs/${runId}/recommendations`,
    (b) => (b.recommendations ?? []).some((r) => !beforeRecs.some((x) => x.id === r.id) && ["COMPLETED", "FAILED"].includes(String(r.status))),
    { timeoutMs: 25 * 60 * 1000, intervalMs: 15_000, label: "s08a rec", key: (b) => JSON.stringify((b.recommendations ?? []).map((r) => `${String(r.id).slice(0, 8)}:${r.status}`)) },
  );
  const rec = (list.recommendations ?? []).find((r) => !beforeRecs.some((x) => x.id === r.id))!;
  const result = (rec.result ?? {}) as Record<string, unknown>;
  const recommended = String(result.recommended_prompt ?? result.system_prompt ?? result.prompt ?? "");
  await record(testInfo, "recommendation.json", rec, `${rec.status} after ${seconds}s prompt_len=${recommended.length}`);
  await page.goto(`/v2/eval/tasks?view=detail&id=${runId}`);
  await expect(page.getByTestId("v2-rec-card").first()).toBeVisible({ timeout: 60_000 });
  await snap(page, "s08a-recommendation", String(rec.status));
  expect(rec.status, `recommendation ended ${rec.status}: ${rec.error ?? ""}`).toBe("COMPLETED");
  expect(recommended.length, "recommended prompt present").toBeGreaterThan(20);
  expect(String(result.explanation ?? result.rationale ?? "").length, "explanation present").toBeGreaterThan(10);

  const depsBefore = (before.deployments as Array<{ job_id: string }>).map((d) => d.job_id);
  await page.getByTestId("v2-rec-accept").first().click();
  const confirm = page.getByTestId("v2-rec-accept-confirm");
  await expect(confirm).toBeVisible({ timeout: 10_000 });
  await snap(page, "s08a-accept-dialog");
  const tAccept = Date.now();
  await confirm.click();
  await page.waitForTimeout(3000);
  let after = await getAgent(api, hr.id);
  const newJob = (after.deployments as Array<{ job_id: string; status: string }>).find((d) => !depsBefore.includes(d.job_id));
  expect(newJob, "a redeploy job appears on the HR assistant").toBeTruthy();
  const job = await waitForJob(api, newJob!.job_id, { timeoutMs: 20 * 60 * 1000, onTick: (j) => ledger(`s08a redeploy ${newJob!.job_id.slice(0, 8)} ${j.status}`) });
  after = await getAgent(api, hr.id);
  await gotoRendered(page, `/v2/agents?view=detail&id=${hr.id}`, String(after.name ?? hr.name)); // N-SCN-obs19
  await snap(page, "s08a-hr-after-accept", `v${after.version} ${after.status} ${Math.round((Date.now() - tAccept) / 1000)}s`, { key: true });
  expect(job.status, `redeploy ended ${job.status}`).toBe("succeeded");
  expect(after.status).toBe("active");
  expect(Number(after.version)).toBe(Number(before.version) + 1);
  expect(String((after.spec as Record<string, unknown>).system_prompt)).not.toBe(promptBefore);
  stateSet("hr", { ...hr, version: Number(after.version) });
  stateSet("improve", { ...(stateGet("improve") ?? {}), recommendation_id: String(rec.id), hr_version_after_accept: Number(after.version) });
  ledger(`s08a PASS rec=${rec.id} ${seconds}s; hr v${before.version} → v${after.version}`);
});

test("s08b — configuration experiments retired (N13-obs2): deep links land on the canary list, `/api/experiments` answers 404", async ({ page }, testInfo) => {
  stepGate("s08");
  test.setTimeout(5 * 60 * 1000);
  const api = page.request;
  // N12-obs49 hid the configuration experiment, N13-obs2 (user decision 2026-10-17) deleted it; the redirect
  // rule is what remains in the console (hard), the routes are gone from the backend (404, not 405 / 5xx)
  await expectConfigurationRedirect(page, "s08b");
  await snap(page, "s08b-configuration-redirect", "?mode=configuration → canary list (N12-obs49 / N13-obs2)", { key: true });
  for (const path of ["/api/experiments", "/api/experiments/readiness?agent_id=x"]) {
    const res = await api.get(path, { timeout: 60_000 });
    ledger(`s08b GET ${path} → ${res.status()}`);
    expect(res.status(), `${path} is retired`).toBe(404);
  }
  const providers = (await (await api.get("/api/eval/recommendation-providers", { timeout: 60_000 })).json()) as { providers: Array<{ id: string }> };
  await record(testInfo, "recommendation-providers.json", providers, `providers=${providers.providers.map((p) => p.id).join(",")}`);
  expect(providers.providers.map((p) => p.id), "the picker's provider discovery moved with the retirement").toContain("gepa_lite");
  ledger("s08b PASS configuration experiment retired (N13-obs2); the runtime twin's improvement loop is s08c");
});

/** N-SCN-obs14 per-arm bar (`Settings.verdict_min_arm_n`): every canary `traffic` round asks the worker for this many sessions per arm. */
const SESSIONS_PER_ARM = 10;

/** Count of traffic attempts over every ramp round of a canary row. */
const trafficAttempts = (row: Record<string, unknown>): number =>
  (((row.artifacts as Record<string, unknown>)?.rounds ?? []) as Array<Record<string, unknown>>).reduce((n, r) => n + ((r.traffic_attempts as unknown[] | undefined)?.length ?? 0), 0);

/**
 * One canary `traffic` round: POST, wait for the action to settle, then insist that the row carries no `error` and that a
 * new attempt was appended at the current weights. `settle(…, "rounds")` alone is satisfied by an earlier stage's round,
 * so a failed round (N-SCN-bug3: the worker's telemetry gate reads the content log of the first endpoint only) used to
 * surface only as a 409 on the following `verdict`.
 * `extra.sessions_per_arm` (N-SCN-obs14) makes the worker cycle the dataset until each arm holds that many sessions, so a
 * 90/10 round can reach the product's per-arm bar (`verdict_min_arm_n`, 10) instead of answering `insufficient-n`.
 */
async function trafficRound(api: APIRequestContext, url: string, datasetId: string, label: string, extra: { sessions_per_arm?: number } = {}): Promise<Record<string, unknown>> {
  const before = trafficAttempts((await (await api.get(url, { timeout: 60_000 })).json()) as Record<string, unknown>);
  expect((await act(api, `${url}/action`, { action: "traffic", dataset_id: datasetId, ...extra }, label)).status).toBeLessThan(300);
  const row = (await pollUntil<Record<string, unknown>>(api, url, (b) => !b.running_action, { timeoutMs: 25 * 60 * 1000, intervalMs: 10_000, label, key: (b) => `${b.status}/${b.stage} running=${b.running_action ?? "-"} progress=${String(b.progress ?? "").slice(0, 100)}` })).body;
  expect(row.error ?? null, `${label}: the traffic action failed on the product side`).toBeNull();
  expect(trafficAttempts(row), `${label}: a traffic attempt was recorded at the current weights`).toBeGreaterThan(before);
  return row;
}

test("s08c — Runtime canary on the runtime twin: setup (stable/treat endpoints) → traffic → verdict → advance 50/50 → complete (or rollback) → cleanup removes the endpoints", async ({ page }, testInfo) => {
  stepGate("s08");
  test.setTimeout(90 * 60 * 1000);
  const api = page.request;
  const runtime = stateRequire("runtime", "s04");
  const ds = stateRequire("dataset", "s07");
  const before = await getAgent(api, runtime.id);
  const basePrompt = String((before.spec as Record<string, unknown>).system_prompt ?? "");
  const candidatePrompt = `${basePrompt}\nAlways end your reply with the word '${TOKENS.canary}'.`;

  // resume: one running canary per agent — a re-run continues the canary an earlier attempt left at its stage
  const existing = (((await (await api.get("/api/runtime-canaries", { timeout: 60_000 })).json()) as { canaries: Array<Record<string, unknown>> }).canaries).find(
    (c) => c.champion_agent_id === runtime.id && c.status === "running" && !(c.artifacts as Record<string, unknown> | undefined)?.cleanup,
  );
  let created: { status: number; body: Record<string, unknown> } = existing ? { status: 201, body: existing } : { status: 0, body: {} };
  if (existing) ledger(`s08c reusing running canary ${existing.id} (stage ${existing.stage})`);
  // N12-obs46 / obs49: CanaryStart through the UI (champion → candidate prompt → evaluators + primary → CREATE → detail), hard — no API fallback
  if (!existing) created = { status: 201, body: await createCanaryViaUi(page, api, { agentId: runtime.id, agentName: runtime.name, candidatePrompt, label: "s08c", onForm: (p) => snap(p, "s08c-canary-form", "champion + candidate + evaluators/primary", { key: true }) }) };
  expect(created.status, JSON.stringify(created.body)).toBe(201);
  const id = String(created.body.id);
  const url = `/api/runtime-canaries/${id}`;
  const detail = `/v2/eval/experiments?mode=canary&canary=${id}`;
  stateSet("improve", { ...(stateGet("improve") ?? {}), canary_id: id });

  const tSetup = Date.now();
  if (!(created.body.artifacts as Record<string, unknown> | undefined)?.setup) {
    expect((await act(api, `${url}/action`, { action: "setup" }, "s08c setup")).status).toBeLessThan(300);
  }
  let row = (await settle(api, url, "abtest", "s08c setup", 30 * 60 * 1000)).body;
  const eps = runtimeEndpoints(runtime.resource_id);
  await record(testInfo, "endpoints-after-setup.json", eps, `setup ${Math.round((Date.now() - tSetup) / 1000)}s endpoints=${eps.map((e) => `${e.name}=${e.status}`).join(",")}`);
  await gotoRendered(page, detail, id); // N-SCN-obs19 (t01 gotoCanary: the canary id text is the rendered marker)
  await snap(page, "s08c-canary-setup", eps.map((e) => e.name).join(","), { key: true });
  expect(String(row.status)).not.toMatch(/failed/i);
  expect(eps.some((e) => e.name.startsWith("stable")), "stable… endpoint exists").toBeTruthy();
  expect(eps.some((e) => e.name.startsWith("treat")), "treat… endpoint exists").toBeTruthy();

  // traffic → verdict, repeated while the verdict is `insufficient-*` — at most 4 rounds. N-SCN-obs14: a directional
  // verdict needs `verdict_min_arm_n` (10) scored records in EACH arm; a plain 90/10 round over nine prompts leaves the
  // treatment arm at ~1 record (run 2026-10-14 on t1: treat 6 / control 65 after four plain rounds), so every round asks
  // the worker for `sessions_per_arm: 10` — the product's own reason text ("send a traffic round with sessions_per_arm ≥ 10")
  let verdictOutcome = "";
  let roundsSent = 0;
  for (let round = 1; round <= 4; round++) {
    row = await trafficRound(api, url, ds.id, `s08c traffic r${round}`, { sessions_per_arm: SESSIONS_PER_ARM });
    roundsSent += 1;
    await gotoRendered(page, detail, id); // N-SCN-obs19
    await snap(page, `s08c-canary-traffic-r${round}`);
    expect((await act(api, `${url}/action`, { action: "verdict" }, `s08c verdict r${round}`)).status).toBeLessThan(300);
    row = (await pollUntil<Record<string, unknown>>(api, url, (b) => !b.running_action, { timeoutMs: 10 * 60 * 1000, label: `s08c verdict r${round}` })).body;
    const rounds = ((row.artifacts as Record<string, unknown>).rounds ?? []) as Array<Record<string, unknown>>;
    const last = (rounds[rounds.length - 1]?.verdict ?? {}) as Record<string, unknown>;
    verdictOutcome = String(last.verdict ?? "");
    ledger(`s08c round ${round}: verdict=${verdictOutcome} n=${last.n ?? "?"} significant=${last.significant ?? "?"} reason=${String(last.reason ?? "").slice(0, 120)}`);
    if (!/insufficient/.test(verdictOutcome)) break;
  }
  if (roundsSent > 1) testInfo.annotations.push({ type: "observation", description: `N-SCN-obs2/obs14: canary needed ${roundsSent} traffic rounds (sessions_per_arm ${SESSIONS_PER_ARM}) before a decidable verdict at 90/10` });
  await record(testInfo, "canary-rounds.json", (row.artifacts as Record<string, unknown>).rounds, `rounds=${roundsSent} verdict=${verdictOutcome}`);
  await gotoRendered(page, detail, id); // N-SCN-obs19
  await snap(page, "s08c-canary-verdict", verdictOutcome);
  expect(verdictOutcome, "a decidable canary verdict (not insufficient-data) within 4 traffic rounds").not.toMatch(/insufficient/);
  // N-SCN-obs22 (lead ruling 2026-10-16): the storyline exercises the canary LIFECYCLE, not a promotion. PASS = the 50/50
  // stage is reached with a verdict that names the primary evaluator (N12-obs46) and the canary is rolled back (or
  // completed) and cleaned. `complete` before the final 1/99 stage must be refused with 409 `canary.stage_not_ready`
  // (expected-behaviour check, hard); `treatment-wins` at every stage → complete at 1/99 is the alternative PASS branch.
  // Only a SIGNIFICANT control-wins at 90/10 (the candidate clearly lost before any ramp) grades `no-improvement`
  // (§6 N-SCN-obs15 wording); a non-significant control-wins at an intermediate stage is overridable advance evidence.
  const lastVerdict = (r: Record<string, unknown>): Record<string, unknown> => {
    const rounds = ((r.artifacts as Record<string, unknown>).rounds ?? []) as Array<Record<string, unknown>>;
    return (rounds[rounds.length - 1]?.verdict ?? {}) as Record<string, unknown>;
  };
  const namesPrimary = (v: Record<string, unknown>): boolean => !!(v.primary ?? v.primary_evaluator) || /primary evaluator/i.test(String(v.reason ?? ""));
  let primaryNamed = namesPrimary(lastVerdict(row));
  let fiftyFiftyReached = false;
  let lostAt90 = false;
  let outcome: "complete" | "rollback" = "rollback";
  if (verdictOutcome === "control-wins" && lastVerdict(row).significant === true) {
    lostAt90 = true;
    testInfo.annotations.push({ type: "no-improvement", description: `canary verdict control-wins, significant, at 90/10 — the candidate lost before any ramp stage → rolled back (§6 N-SCN-obs15 / obs22)` });
    ledger("s08c verdict control-wins (significant) at 90/10 → the ramp policy refuses advance; rolling back");
  } else {
    // the ramp (RAMP_WEIGHTS 90/10 → 50/50 → 1/99): every stage needs traffic → verdict at its weights before the
    // next advance; the final stage is completed, not advanced (canary_service.stage_not_ready_reason)
    const FINAL_STAGE = 1; // loop index after the second advance = 1/99
    let rampExhausted = false;
    let stage = 0;
    for (; stage < 4; stage++) {
      const adv = await act(api, `${url}/action`, { action: "advance", allow_non_significant: true }, `s08c advance s${stage}`);
      if (adv.status >= 300 && /final .* stage must be completed/i.test(JSON.stringify(adv.body))) { rampExhausted = true; break; }
      expect(adv.status, `advance from ramp stage ${stage} accepted (allow_non_significant)`).toBeLessThan(300);
      row = (await pollUntil<Record<string, unknown>>(api, url, (b) => !b.running_action, { timeoutMs: 10 * 60 * 1000, label: `s08c advance s${stage}` })).body;
      await gotoRendered(page, detail, id); // N-SCN-obs19
      await snap(page, `s08c-canary-advanced-s${stage + 1}`, JSON.stringify((row.artifacts as Record<string, unknown>).abtest).slice(0, 60));
      // traffic + verdict at the new weights (loop while insufficient-*, ≤ 3 rounds; the per-arm bar applies here too)
      let v = "";
      let last: Record<string, unknown> = {};
      for (let round = 1; round <= 3; round++) {
        row = await trafficRound(api, url, ds.id, `s08c traffic s${stage + 1}r${round}`, { sessions_per_arm: SESSIONS_PER_ARM });
        roundsSent += 1;
        expect((await act(api, `${url}/action`, { action: "verdict" }, `s08c verdict s${stage + 1}r${round}`)).status).toBeLessThan(300);
        row = (await pollUntil<Record<string, unknown>>(api, url, (b) => !b.running_action, { timeoutMs: 10 * 60 * 1000, label: `s08c verdict s${stage + 1}r${round}` })).body;
        last = lastVerdict(row);
        v = String(last.verdict ?? "");
        ledger(`s08c stage ${stage + 1} round ${round}: verdict=${v} n=${last.n ?? "?"} significant=${last.significant ?? "?"} primary=${last.primary ?? last.primary_evaluator ?? "?"}`);
        if (!/insufficient/.test(v)) break;
      }
      primaryNamed = primaryNamed || namesPrimary(last);
      if (stage === 0 && !/insufficient/.test(v)) fiftyFiftyReached = true;
      if (v !== "treatment-wins") {
        if (stage < FINAL_STAGE) {
          // expected behaviour: `complete` is refused before the final 1/99 stage
          const early = await act(api, `${url}/action`, { action: "complete", allow_non_significant: true }, `s08c complete at stage ${stage + 1} (expected 409)`);
          expect(early.status, "complete before the final 1/99 stage is refused").toBe(409);
          expect(String(early.body.code), "canary.stage_not_ready").toBe("canary.stage_not_ready");
          ledger(`s08c stage ${stage + 1}: verdict ${v} (significant=${last.significant}) → complete refused 409 canary.stage_not_ready as expected → rollback (N-SCN-obs22 PASS path)`);
        } else {
          ledger(`s08c final stage: verdict ${v} (significant=${last.significant}) → not a treatment win; rolling back`);
        }
        break;
      }
      verdictOutcome = v;
    }
    if (rampExhausted) {
      // treatment-wins at every stage → the alternative PASS branch: complete at 1/99
      const complete = await act(api, `${url}/action`, { action: "complete", allow_non_significant: true }, "s08c complete");
      expect(complete.status, `complete at the final stage (${JSON.stringify(complete.body).slice(0, 120)})`).toBeLessThan(300);
      row = (await settle(api, url, "complete", "s08c complete", 20 * 60 * 1000)).body;
      outcome = "complete";
    }
  }
  if (outcome === "rollback") {
    expect((await act(api, `${url}/action`, { action: "rollback" }, "s08c rollback")).status).toBeLessThan(300);
    row = (await settle(api, url, "rollback", "s08c rollback", 20 * 60 * 1000)).body;
  }
  const after = await getAgent(api, runtime.id);
  await gotoRendered(page, detail, id); // N-SCN-obs19
  await snap(page, `s08c-canary-${outcome}`, `v${after.version}`);
  expect((await act(api, `${url}/action`, { action: "cleanup" }, "s08c cleanup")).status).toBeLessThan(300);
  row = (await settle(api, url, "cleanup", "s08c cleanup", 10 * 60 * 1000)).body;
  // endpoint deletion is asynchronous on the Runtime side (DELETING → gone): poll the read-only list up to 5 min
  let epsAfter = runtimeEndpoints(runtime.resource_id);
  const tGone = Date.now();
  while (epsAfter.some((e) => e.name.startsWith("stable") || e.name.startsWith("treat")) && Date.now() - tGone < 5 * 60 * 1000) {
    ledger(`s08c endpoints after cleanup +${Math.round((Date.now() - tGone) / 1000)}s: ${epsAfter.map((e) => `${e.name}=${e.status}`).join(",")}`);
    await new Promise((r) => setTimeout(r, 15_000));
    epsAfter = runtimeEndpoints(runtime.resource_id);
  }
  await record(testInfo, "endpoints-after-cleanup.json", epsAfter, `${Math.round((Date.now() - tGone) / 1000)}s after cleanup: ${epsAfter.map((e) => `${e.name}=${e.status}`).join(",") || "(none)"}`);
  expect(epsAfter.some((e) => e.name.startsWith("stable") || e.name.startsWith("treat")), "canary endpoints removed (≤ 5 min after cleanup)").toBeFalsy();
  stateSet("runtime", { ...runtime, version: Number(after.version) });
  // N-SCN-obs22 grading: rollback is PASS when the 50/50 stage was reached (or the candidate lost significantly at 90/10,
  // `no-improvement`), the verdicts named the primary evaluator and the cleanup removed the endpoints (asserted above)
  if (outcome === "rollback" && !fiftyFiftyReached && !lostAt90) testInfo.annotations.push({ type: "deviation", description: "canary rolled back without reaching the 50/50 stage and without a significant 90/10 loss (N-SCN-obs22 PASS criterion not met)" });
  if (!primaryNamed) testInfo.annotations.push({ type: "deviation", description: "no canary verdict named the primary evaluator (N12-obs46 / N-SCN-obs22)" });
  stateSet("improve", { ...(stateGet("improve") ?? {}), canary_id: id, canary_outcome: outcome });
  ledger(`s08c PASS canary=${id} outcome=${outcome} fiftyFifty=${fiftyFiftyReached} primaryNamed=${primaryNamed} runtime v${before.version} → v${after.version} status=${row.status}`);
});

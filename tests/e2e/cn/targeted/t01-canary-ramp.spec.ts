import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { awsRead, evidence, getAgent, ledger, listAgents, sleep, waitForDeploy } from "../helpers";
import { softly, act, FIFTEEN_MIN, GATEWAY_TARGET, pollUntil, PROMPTS, record, runtimeEndpoints, scnName, settle, snap, stateGet, stateSet } from "../scenario/scn";

/**
 * t01 — TARGETED canary verification (lead ruling after counting run #10).
 *
 * Counting run #10 passed 29/29 but its canary candidate ("end every reply with Cheers!") lost at
 * 90/10 (control-wins, n = 36, significant) and was rolled back by design, so the 50/50 and 1/99
 * ramp stages — and the N-SCN-bug3 treat-arm-last telemetry case — stayed unexercised. This spec
 * re-tests that path alone (≤ 45 min): deploy the zip-runtime twin (gateway only, no KB) → a
 * 9-prompt replay dataset → canary with a quality-improving candidate → traffic / verdict per
 * ramp stage (90/10 → 50/50 → 1/99) → `complete` → cleanup, asserting per stage that the per-arm
 * telemetry wait reaches `log group 2/2`, the verdict shape, and the advance / complete semantics.
 * A candidate that still loses is recorded as `no-improvement` (PASS) with the verdict numbers.
 *
 * N-SCN-obs15 (user ruling 2026-10-14): at an intermediate stage a NON-significant `control-wins`
 * is noise like a tie — the spec advances through it with `allow_non_significant: true` (ledger
 * line) and only a SIGNIFICANT `control-wins` grades `no-improvement`; at the final 1/99 stage
 * `complete` is refused on ANY `control-wins` (nothing to promote), so the run rolls back there.
 */
test.describe.configure({ mode: "serial" });

const CANDIDATE_SUFFIX =
  "\nStructure every answer in three short parts: (1) the direct answer first, with the key figure or name; " +
  "(2) the source you relied on (the tool record you fetched or the policy rule); (3) one concrete next step the employee can take. " +
  "Be complete and specific, never leave part of the question unanswered, and when a tool can answer the question call it before replying.";

const REPLAY_PROMPTS: Array<[string, string]> = [
  ["magic-word", "What is the magic word? Reply with it and one sentence on what you can help with."],
  ["emp-1024-lookup", "Look up employee EMP-1024: what are their name, department and remaining vacation days this year?"],
  ["departments", "Which departments exist at Octank? List them for me."],
  ["vacation-plan", "EMP-1024 wants to take a week off next month — how many vacation days do they have left and how should they request the leave?"],
  ["calendar", "Check the calendar for EMP-1024 for the coming week and tell me what is scheduled."],
  ["payout-info", "I need to request a payout of 500 for EMP-1024. What information do you need from me and what happens next?"],
  ["expense-claim", "How do I file an expense claim at Octank, and who approves it?"],
  ["capabilities", "Summarize in a few bullet points what you can do for an Octank employee."],
  ["new-hire", "I joined Octank last week. What should I do first in the HR self-service, and whom do I contact for payroll questions?"],
];

interface StageRecord {
  stage: number;
  weights: unknown;
  traffic_seconds: number;
  rounds_sent: number;
  saw_both_log_groups: boolean;
  verdict: string;
  n: unknown;
  significant: unknown;
  /** final stage only: `sessions_per_arm` top-up rounds sent before `complete` (N-SCN-obs14) */
  top_up_rounds?: number;
}

const trafficAttempts = (row: Record<string, unknown>): number =>
  (((row.artifacts as Record<string, unknown>)?.rounds ?? []) as Array<Record<string, unknown>>).reduce((n, r) => n + ((r.traffic_attempts as unknown[] | undefined)?.length ?? 0), 0);

/** One traffic round; samples `progress` every 10 s and reports whether the per-arm wait reached `log group 2/2` (N-SCN-bug3). */
async function trafficRound(api: APIRequestContext, url: string, datasetId: string, label: string, extra: { sessions_per_arm?: number } = {}): Promise<{ row: Record<string, unknown>; sawBoth: boolean; seconds: number }> {
  const before = trafficAttempts((await (await api.get(url, { timeout: 60_000 })).json()) as Record<string, unknown>);
  expect((await act(api, `${url}/action`, { action: "traffic", dataset_id: datasetId, ...extra }, label)).status).toBeLessThan(300);
  const t0 = Date.now();
  let sawBoth = false;
  let lastProgress = "";
  let row: Record<string, unknown> = {};
  while (Date.now() - t0 < 25 * 60 * 1000) {
    const res = await api.get(url, { timeout: 60_000 }).catch(() => null);
    if (res?.ok()) {
      row = (await res.json()) as Record<string, unknown>;
      const progress = String(row.progress ?? "");
      if (progress && progress !== lastProgress) {
        lastProgress = progress;
        ledger(`${label} +${Math.round((Date.now() - t0) / 1000)}s ${progress.slice(0, 140)}`);
      }
      if (/log group 2\/2/.test(progress)) sawBoth = true;
      if (!row.running_action) break;
    }
    await sleep(10_000);
  }
  const seconds = Math.round((Date.now() - t0) / 1000);
  expect(row.error ?? null, `${label}: the traffic action failed on the product side`).toBeNull();
  expect(trafficAttempts(row), `${label}: a traffic attempt was recorded at the current weights`).toBeGreaterThan(before);
  if (!sawBoth) ledger(`soft: ${label} never showed 'log group 2/2' in the sampled progress (sampled every 10 s)`);
  return { row, sawBoth, seconds };
}

/**
 * Open the canary detail and wait for the row to render before a `snap`: the V2 page fetches after `load`, so a bare
 * `goto` + screenshot captured the loading spinner in every earlier run (confirmation run 2026-10-14). Evidence only —
 * a slow render never fails the test.
 */
async function gotoCanary(page: Page, detail: string, id: string): Promise<void> {
  await page.goto(detail);
  await page.getByText(id, { exact: true }).first().waitFor({ state: "visible", timeout: 60_000 }).catch(() => {});
}

function lastVerdict(row: Record<string, unknown>): Record<string, unknown> {
  const rounds = ((row.artifacts as Record<string, unknown>).rounds ?? []) as Array<Record<string, unknown>>;
  return (rounds[rounds.length - 1]?.verdict ?? {}) as Record<string, unknown>;
}

test("t01a — twin for the canary: zip_runtime HR runtime (gateway only) deploys ≤ 15 min; a 9-prompt replay dataset is created", async ({ page }, testInfo) => {
  test.setTimeout(40 * 60 * 1000);
  const api = page.request;
  const health = await api.get("/api/health", { timeout: 60_000 });
  expect(health.status(), "GET /api/health").toBe(200);
  const name = scnName("canary-twin");
  const prompt = PROMPTS.hrRuntime;

  await page.goto("/v2/agents");
  await page.getByTestId("v2-agent-new").click();
  await page.getByTestId("v2-agent-method-zip_runtime").click();
  await page.getByTestId("v2-agent-wizard-next").click();
  await page.getByTestId("v2-agent-name").fill(name);
  await page.getByTestId("v2-agent-prompt").fill(prompt);
  const gw = page.getByTestId("v2-agent-gateways").locator("label", { hasText: GATEWAY_TARGET }).locator('input[type="checkbox"]');
  await expect(gw, "gateway target offered to the zip runtime").toBeEnabled({ timeout: 60_000 });
  await gw.check();
  await evidence(page, "t01-twin-wizard");
  const review = page.getByTestId("v2-agent-review");
  await expect
    .poll(async () => {
      if (await review.isVisible().catch(() => false)) return true;
      await page.getByTestId("v2-agent-wizard-next").click().catch(() => {});
      await page.waitForTimeout(2000);
      return review.isVisible().catch(() => false);
    }, { timeout: 60_000, intervals: [1000], message: "wizard reaches the review step" })
    .toBe(true);
  const t0 = Date.now();
  await page.getByTestId("v2-agent-wizard-submit").click();
  await expect(page).toHaveURL(/view=detail&id=/, { timeout: 60_000 });
  const id = new URL(page.url()).searchParams.get("id")!;
  const dep = await waitForDeploy(page, testInfo, id, "t01a", t0);
  expect(dep.job.status, `deploy failed: ${dep.job.error ?? ""}`).toBe("succeeded");
  expect(dep.agent.status).toBe("active");
  expect(dep.seconds, "zip runtime active ≤ 15 min").toBeLessThanOrEqual(FIFTEEN_MIN);
  const full = await getAgent(api, id);
  const resourceId = String(full.resource_id ?? String(full.arn ?? "").split("/").pop() ?? "");
  stateSet("runtime", { id, name, resource_id: resourceId, version: Number(full.version ?? 1), deploy_seconds: dep.seconds });

  // replay dataset: legacy prompt items (resolve_traffic_items reads `prompt`), one scenario label each
  const dsName = scnName("canary-ds");
  const created = await api.post("/api/eval/datasets", {
    data: { name: dsName, description: "targeted canary verification — HR self-service prompts for the twin", items: REPLAY_PROMPTS.map(([sid, p]) => ({ scenario_id: sid, prompt: p })) },
    timeout: 60_000,
  });
  const body = (await created.json().catch(async () => ({ raw: await created.text() }))) as { id?: string; dataset?: { id: string }; items?: unknown[] };
  expect(created.status(), JSON.stringify(body).slice(0, 300)).toBeLessThan(300);
  const dsId = String(body.id ?? body.dataset?.id ?? "");
  expect(dsId, "dataset id").toBeTruthy();
  stateSet("dataset", { id: dsId, name: dsName, items: REPLAY_PROMPTS.length });
  await record(testInfo, "dataset.json", body, `dataset ${dsId} with ${REPLAY_PROMPTS.length} prompts`);
  ledger(`t01a PASS twin=${id} (${resourceId}) deploy=${dep.seconds}s dataset=${dsId}`);
});

test("t01b — canary ramp with a quality-improving candidate: setup → [traffic → verdict → advance] × stages → complete (or no-improvement rollback) → cleanup", async ({ page }, testInfo) => {
  test.setTimeout(75 * 60 * 1000);
  const api = page.request;
  const runtime = stateGet("runtime")!;
  const ds = stateGet("dataset")!;
  const before = await getAgent(api, runtime.id);
  const basePrompt = String((before.spec as Record<string, unknown>).system_prompt ?? "");
  const candidatePrompt = `${basePrompt}${CANDIDATE_SUFFIX}`;

  const created = await act(api, "/api/runtime-canaries", { agent_id: runtime.id, candidate: { system_prompt: candidatePrompt } }, "t01b create");
  expect(created.status, JSON.stringify(created.body)).toBe(201);
  const id = String(created.body.id);
  const url = `/api/runtime-canaries/${id}`;
  const detail = `/v2/eval/experiments?mode=canary&canary=${id}`;
  stateSet("improve", { ...(stateGet("improve") ?? {}), canary_id: id });

  const tSetup = Date.now();
  expect((await act(api, `${url}/action`, { action: "setup" }, "t01b setup")).status).toBeLessThan(300);
  let row = (await settle(api, url, "abtest", "t01b setup", 30 * 60 * 1000)).body;
  const setupArt = ((row.artifacts as Record<string, unknown>).setup ?? {}) as Record<string, unknown>;
  const eps = runtimeEndpoints(runtime.resource_id);
  await record(testInfo, "endpoints-after-setup.json", eps, `setup ${Math.round((Date.now() - tSetup) / 1000)}s endpoints=${eps.map((e) => `${e.name}=${e.status}`).join(",")} v_current=${setupArt.v_current} v_candidate=${setupArt.v_candidate}`);
  await gotoCanary(page, detail, id);
  await snap(page, "t01-canary-setup", eps.map((e) => e.name).join(","), { key: true });
  expect(String(row.status)).not.toMatch(/failed/i);
  expect(eps.some((e) => e.name.startsWith("stable")), "stable… endpoint exists").toBeTruthy();
  expect(eps.some((e) => e.name.startsWith("treat")), "treat… endpoint exists").toBeTruthy();

  // N-SCN-obs14: a directional verdict needs `min_arm_n` scored records in EACH arm, so a
  // 90/10 round over nine prompts (≈ 1 treatment session) answers `insufficient-n` carrying
  // `arms_n` / `min_arm_n`. That IS the stage's decision — weak evidence — and the ramp
  // policy lets the operator advance through it with the override (the next stage feeds the
  // starved arm); only `insufficient-data` (no scores at all) keeps the round loop going.
  const stages: StageRecord[] = [];
  let outcome: "complete" | "rollback" = "rollback";
  let stage = 0;
  for (;;) {
    const weights = (((row.artifacts as Record<string, unknown>).setup ?? {}) as Record<string, unknown>).weights;
    let verdictOutcome = "";
    let v: Record<string, unknown> = {};
    let roundsSent = 0;
    let sawBoth = false;
    let trafficSeconds = 0;
    for (let round = 1; round <= 3; round++) {
      const r = await trafficRound(api, url, ds.id, `t01b traffic s${stage}r${round}`);
      roundsSent += 1;
      sawBoth = sawBoth || r.sawBoth;
      trafficSeconds += r.seconds;
      expect((await act(api, `${url}/action`, { action: "verdict" }, `t01b verdict s${stage}r${round}`)).status).toBeLessThan(300);
      row = (await pollUntil<Record<string, unknown>>(api, url, (b) => !b.running_action, { timeoutMs: 10 * 60 * 1000, label: `t01b verdict s${stage}r${round}` })).body;
      v = lastVerdict(row);
      verdictOutcome = String(v.verdict ?? "");
      const armsN = (v.arms_n ?? null) as { control?: number; treatment?: number } | null;
      ledger(`t01b stage ${stage} (${JSON.stringify(weights)}) round ${round}: verdict=${verdictOutcome} n=${v.n ?? "?"} significant=${v.significant ?? "?"} arms=C ${armsN?.control ?? "?"}/${v.min_arm_n ?? "?"} T1 ${armsN?.treatment ?? "?"}/${v.min_arm_n ?? "?"} both_log_groups=${sawBoth}`);
      if (!/insufficient/.test(verdictOutcome)) break;
      if (verdictOutcome === "insufficient-n" && armsN) break; // decided: an arm is below the bar
    }
    stages.push({ stage, weights, traffic_seconds: trafficSeconds, rounds_sent: roundsSent, saw_both_log_groups: sawBoth, verdict: verdictOutcome, n: v.n, significant: v.significant });
    await gotoCanary(page, detail, id);
    await snap(page, `t01-canary-stage${stage}-verdict`, `${verdictOutcome} n=${v.n ?? "?"}`, { key: stage > 0 });
    expect(verdictOutcome, `stage ${stage}: a decidable verdict within 3 traffic rounds`).toMatch(/^(treatment-wins|control-wins|tie|insufficient-n)$/);
    if (verdictOutcome === "insufficient-n") {
      // N-SCN-obs14 verdict shape: both arms' counts and the bar, named in the reason
      expect(v.arms_n, `stage ${stage}: insufficient-n carries arms_n`).toEqual(expect.objectContaining({ control: expect.any(Number), treatment: expect.any(Number) }));
      expect(v.min_arm_n, `stage ${stage}: insufficient-n carries min_arm_n`).toEqual(expect.any(Number));
      expect(String(v.reason ?? ""), `stage ${stage}: reason names the per-arm bar`).toMatch(/scored records per arm/);
    } else {
      expect(v.arms_n, `stage ${stage}: a directional verdict carries arms_n`).toEqual(expect.objectContaining({ control: expect.any(Number), treatment: expect.any(Number) }));
    }
    softly(testInfo, `stage ${stage}: per-arm telemetry wait reached 'log group 2/2' (N-SCN-bug3)`, () => expect(sawBoth, `stage ${stage}: per-arm telemetry wait reached 'log group 2/2' (N-SCN-bug3)`).toBe(true));
    if (verdictOutcome === "control-wins" && v.significant !== false) {
      testInfo.annotations.push({ type: "no-improvement", description: `canary candidate lost at ramp stage ${stage} (${JSON.stringify(weights)}): control-wins n=${v.n} significant=${v.significant} → rolled back by design` });
      ledger(`t01b significant control-wins at stage ${stage} → the ramp policy refuses advance; rolling back`);
      break;
    }
    if (verdictOutcome === "control-wins" && stage < 2) {
      // N-SCN-obs15: non-significant control-wins is overridable advance evidence like a tie
      ledger(`t01b non-significant control-wins at stage ${stage} (n=${v.n}) → overridable per N-SCN-obs15; advancing with allow_non_significant`);
    }
    if (stage >= 2) {
      // N-SCN-obs14 (r7-m): `complete` is terminal — insufficient-n is blocked even with the
      // override, so feed the starved arm with `sessions_per_arm = min_arm_n` (≤ 2 extra rounds)
      // and re-verdict before promoting; earlier stages advance through it with the override.
      let topUps = 0;
      while (verdictOutcome === "insufficient-n" && topUps < 2) {
        topUps += 1;
        const perArm = Number(v.min_arm_n ?? 10);
        const r = await trafficRound(api, url, ds.id, `t01b top-up s${stage}t${topUps} (sessions_per_arm=${perArm})`, { sessions_per_arm: perArm });
        roundsSent += 1;
        sawBoth = sawBoth || r.sawBoth;
        trafficSeconds += r.seconds;
        expect((await act(api, `${url}/action`, { action: "verdict" }, `t01b verdict s${stage}t${topUps}`)).status).toBeLessThan(300);
        row = (await pollUntil<Record<string, unknown>>(api, url, (b) => !b.running_action, { timeoutMs: 10 * 60 * 1000, label: `t01b verdict s${stage}t${topUps}` })).body;
        v = lastVerdict(row);
        verdictOutcome = String(v.verdict ?? "");
        const armsN = (v.arms_n ?? null) as { control?: number; treatment?: number } | null;
        ledger(`t01b stage ${stage} top-up ${topUps} (sessions_per_arm=${perArm}): verdict=${verdictOutcome} n=${v.n ?? "?"} significant=${v.significant ?? "?"} arms=C ${armsN?.control ?? "?"}/${v.min_arm_n ?? "?"} T1 ${armsN?.treatment ?? "?"}/${v.min_arm_n ?? "?"}`);
      }
      Object.assign(stages[stages.length - 1], { traffic_seconds: trafficSeconds, rounds_sent: roundsSent, saw_both_log_groups: sawBoth, verdict: verdictOutcome, n: v.n, significant: v.significant, top_up_rounds: topUps });
      if (topUps > 0) {
        await gotoCanary(page, detail, id);
        await snap(page, `t01-canary-stage${stage}-topup-verdict`, `${verdictOutcome} n=${v.n ?? "?"} top_ups=${topUps}`);
      }
      if (verdictOutcome === "control-wins") {
        testInfo.annotations.push({ type: "no-improvement", description: `canary candidate lost at the final stage after ${topUps} top-up round(s): control-wins n=${v.n} significant=${v.significant} → rolled back by design` });
        // N-SCN-obs15: complete is refused on ANY control-wins (significant or not) — assert it once
        const refusedCw = await act(api, `${url}/action`, { action: "complete", allow_non_significant: true }, "t01b complete on control-wins (expect 409)");
        expect(refusedCw.status, "complete is refused on control-wins even with the override").toBe(409);
        ledger(`t01b control-wins at the final stage (significant=${v.significant}) → the ramp policy refuses complete (409); rolling back`);
        break;
      }
      if (verdictOutcome === "insufficient-n") {
        // still below the bar after the bounded top-up: assert the terminal rule (409 even with the
        // override) and roll back — soft, so cleanup still runs and the run reports the shortfall
        const refused = await act(api, `${url}/action`, { action: "complete", allow_non_significant: true }, "t01b complete on insufficient-n (expect 409)");
        expect(refused.status, "complete is refused on insufficient-n even with the override").toBe(409);
        expect(String(refused.body.message ?? ""), "the 409 names sessions_per_arm and both arms").toMatch(/sessions_per_arm/);
        softly(testInfo, `final stage: both arms reached min_arm_n within ${topUps} top-up round(s)`, () => expect(verdictOutcome, `final stage: both arms reached min_arm_n within ${topUps} top-up round(s)`).not.toBe("insufficient-n"));
        break;
      }
      const nonSignificantFinal = verdictOutcome === "tie" || v.significant === false;
      // final 1/99 stage: complete (allow_non_significant when the evidence is weak — the console's confirm-gated path)
      const complete = await act(api, `${url}/action`, { action: "complete", ...(nonSignificantFinal ? { allow_non_significant: true } : {}) }, "t01b complete");
      expect(complete.status, `complete accepted at the final stage (${verdictOutcome}${nonSignificantFinal ? ", non-significant override" : ""})`).toBeLessThan(300);
      row = (await settle(api, url, "complete", "t01b complete", 20 * 60 * 1000)).body;
      outcome = "complete";
      break;
    }
    const nonSignificant = verdictOutcome === "tie" || verdictOutcome === "insufficient-n" || v.significant === false;
    // intermediate stage: advance (the product refuses `complete` here — assert the semantics once)
    if (stage === 1) {
      const early = await act(api, `${url}/action`, { action: "complete", allow_non_significant: true }, "t01b complete-before-final (expect 409)");
      expect(early.status, "complete is refused before the final ramp stage").toBe(409);
    }
    const adv = await act(api, `${url}/action`, { action: "advance", ...(nonSignificant ? { allow_non_significant: true } : {}) }, `t01b advance s${stage}`);
    expect(adv.status, `advance from stage ${stage} accepted (${verdictOutcome}${nonSignificant ? ", non-significant override" : ""})`).toBeLessThan(300);
    row = (await pollUntil<Record<string, unknown>>(api, url, (b) => !b.running_action, { timeoutMs: 10 * 60 * 1000, label: `t01b advance s${stage}` })).body;
    stage += 1;
    const newWeights = (((row.artifacts as Record<string, unknown>).setup ?? {}) as Record<string, unknown>).weights;
    ledger(`t01b advanced to stage ${stage}: weights=${JSON.stringify(newWeights)}`);
    await gotoCanary(page, detail, id);
    await snap(page, `t01-canary-stage${stage}-weights`, JSON.stringify(newWeights));
  }
  await record(testInfo, "stages.json", stages, `stages=${stages.length} outcome=${outcome}`);

  if (outcome === "rollback") {
    expect((await act(api, `${url}/action`, { action: "rollback" }, "t01b rollback")).status).toBeLessThan(300);
    row = (await settle(api, url, "rollback", "t01b rollback", 20 * 60 * 1000)).body;
  }
  const after = await getAgent(api, runtime.id);
  await gotoCanary(page, detail, id);
  await snap(page, `t01-canary-${outcome}`, `v${after.version}`, { key: true });
  if (outcome === "complete") {
    expect(String(row.status), "canary completed").toMatch(/complet|promot/i);
    expect(Number(after.version), "production = the candidate version after complete").toBe(Number(setupArt.v_candidate));
    expect(String((after.spec as Record<string, unknown>).system_prompt ?? ""), "production prompt is the candidate prompt").toContain("Structure every answer in three short parts");
  } else {
    expect(String((after.spec as Record<string, unknown>).system_prompt ?? ""), "production prompt unchanged after rollback").not.toContain("Structure every answer in three short parts");
  }
  expect((await act(api, `${url}/action`, { action: "cleanup" }, "t01b cleanup")).status).toBeLessThan(300);
  row = (await settle(api, url, "cleanup", "t01b cleanup", 10 * 60 * 1000)).body;
  let epsAfter = runtimeEndpoints(runtime.resource_id);
  const tGone = Date.now();
  while (epsAfter.some((e) => e.name.startsWith("stable") || e.name.startsWith("treat")) && Date.now() - tGone < 5 * 60 * 1000) {
    await sleep(15_000);
    epsAfter = runtimeEndpoints(runtime.resource_id);
  }
  await record(testInfo, "endpoints-after-cleanup.json", epsAfter, epsAfter.map((e) => `${e.name}=${e.status}`).join(","));
  expect(epsAfter.some((e) => e.name.startsWith("stable") || e.name.startsWith("treat")), "canary endpoints removed (≤ 5 min after cleanup)").toBeFalsy();
  stateSet("improve", { ...(stateGet("improve") ?? {}), canary_id: id, canary_outcome: outcome });
  ledger(`t01b PASS canary=${id} outcome=${outcome} stages=${stages.map((s) => `${s.stage}:${s.verdict}(n=${s.n},sig=${s.significant},both=${s.saw_both_log_groups})`).join(" ")} twin v${before.version} → v${after.version}`);
});

test("t01c — cleanup: dataset and twin removed through the product; read-only AWS shows no runtime / role / log group", async ({ page }, testInfo) => {
  test.setTimeout(30 * 60 * 1000);
  const api = page.request;
  const runtime = stateGet("runtime")!;
  const ds = stateGet("dataset");
  const done: Array<{ kind: string; id: string; status: number }> = [];
  if (ds) {
    const r = await api.delete(`/api/eval/datasets/${ds.id}`, { timeout: 60_000 });
    done.push({ kind: "dataset", id: ds.id, status: r.status() });
  }
  const res = await api.delete(`/api/agents/${runtime.id}`, { timeout: 180_000 });
  const body = (await res.json().catch(() => ({}))) as { removed?: string[]; failed?: string[] };
  done.push({ kind: "agent", id: runtime.id, status: res.status() });
  ledger(`t01c agent ${runtime.name} DELETE → ${res.status()} removed=${JSON.stringify(body.removed)} failed=${JSON.stringify(body.failed)}`);
  expect(res.status()).toBe(200);
  expect(body.failed ?? [], "no failed teardown step").toEqual([]);
  if ((body.removed ?? []).includes("log_groups")) expect(body.removed![body.removed!.length - 1], "log_groups is the LAST teardown step").toBe("log_groups");
  for (let i = 0; i < 30 && (await listAgents(api)).some((a) => a.id === runtime.id); i++) await sleep(10_000);

  const runtimes = awsRead("bedrock-agentcore-control list-agent-runtimes");
  const left = (runtimes.match(/"agentRuntimeName":\s*"e2es[^"]*"/g) ?? []).map((s) => s.split('"')[3]);
  const roles = awsRead(`iam list-roles --query "Roles[?starts_with(RoleName, 'launchpad-agent-e2es')].RoleName"`);
  const group = `/aws/bedrock-agentcore/runtimes/${runtime.resource_id}`;
  const listLeft = () => awsRead(`logs describe-log-groups --log-group-name-prefix ${group} --query "logGroups[].logGroupName"`);
  let logs = listLeft();
  for (let i = 0; i < 8 && logs.includes(group); i++) {
    ledger(`t01c log groups still listed (+${15 * (i + 1)}s): ${logs.slice(0, 200)}`);
    await sleep(15_000);
    logs = listLeft();
  }
  await record(testInfo, "cleanup-summary.json", { deleted: done, runtimes_left: left, roles_left: roles.slice(0, 300), log_groups_left: logs.slice(0, 300) }, `deleted ${done.length}; runtimes=${left.length}`);
  expect(left, "no e2es runtime on AWS").toEqual([]);
  expect(/launchpad-agent-e2es/.test(roles), "no e2es execution role on AWS").toBe(false);
  expect(logs.includes(group), "twin log groups gone").toBe(false);
  ledger(`t01c PASS cleanup ${done.length} deletions, 0 residue`);
});

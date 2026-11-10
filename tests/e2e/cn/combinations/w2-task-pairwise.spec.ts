/**
 * TaskWizard pairwise (round 16, r16-n step 1b). Every all-pairs row of the task wizard's
 * option domain (wizard-domains.ts) is driven through the real wizard; the suite asserts
 * that the wizard blocks / explains what the server would refuse, and that what it submits
 * the server accepts (201) with a run / online config that reflects the choices. Runs are
 * real but small (3-item datasets, 3 sessions); every created task carries the x16n- prefix
 * and is stopped + deleted by the cleanup spec (a handful are kept for the page suites).
 *
 * Needs the w1 fixture agent `x16n-fx-harness` ACTIVE (waits up to 25 min for the deploy).
 */
import { expect, test, type Page } from "@playwright/test";

import * as L from "./lib";
import { allPairs, type Combo, comboLabel } from "./pairwise";
import * as dom from "./wizard-domains";

const SEED = Number(process.env.X16N_SEED ?? 16);
const FX_AGENT = process.env.X16N_TASK_AGENT ?? "x16n-fx-harness";
const SUITE = "wizard-task";
const KEEP_RUNS = Number(process.env.X16N_KEEP_RUNS ?? 12);
/** X16N_ONLY_ROWS=0,3,4 reruns the named row indices only */
const ONLY_ROWS = (process.env.X16N_ONLY_ROWS ?? "").split(",").filter(Boolean).map(Number);
const NON_GT = ["Builtin.Helpfulness", "Builtin.Correctness", "Builtin.Faithfulness", "Builtin.ResponseRelevance", "Builtin.ContextRelevance", "Builtin.Conciseness", "Builtin.Coherence", "Builtin.InstructionFollowing", "Builtin.Refusal", "Builtin.Harmfulness"];
const GT = "Builtin.TrajectoryInOrderMatch";
const DEFAULT_EVALS = ["Builtin.Correctness", "Builtin.Helpfulness"];

// tests run in file order (workers: 1, fullyParallel: false); NOT serial mode — a soft failure in one test must not skip the rest

let admin: L.Session;
let agent: L.AgentRow;
let datasets: { gt: { id: string; has_ground_truth: boolean }; sim: { id: string } };
let cw: { service_name: string; log_group_names: string[] } | null = null;
let sessionTimes: number[] = [];
const created: { kind: "run" | "online"; id: string; keep: boolean }[] = [];

test.beforeAll(async ({ browser }) => {
  admin = await L.session(browser, "admin");
  const a = await L.waitForAgent(admin.api, FX_AGENT, "active", 25 * 60_000);
  if (!a) throw new Error(`fixture agent ${FX_AGENT} not active`);
  agent = a;
  // three fresh console sessions (insights needs >= 3; the 1 h window must see them)
  const have = ((await admin.api.get("/api/observability/sessions?range=7d")).body?.sessions ?? []).filter((s: { agent: string }) => s.agent === FX_AGENT);
  for (let i = have.length; i < 3; i += 1) {
    const r = await L.chatOnce(admin.api, agent.id, `x16n session ${i + 1}: what is ${i + 2} plus ${i + 2}?`);
    L.log("chat", r.status, r.session_id);
    sessionTimes.push(Date.now());
  }
  datasets = {
    gt: await L.ensureDataset(admin.api, "x16n-ds-gt", L.GT_DATASET_ITEMS),
    sim: await L.ensureDataset(admin.api, "x16n-ds-sim", L.SIM_DATASET_ITEMS),
  };
  const services = (await admin.api.get("/api/eval/log-services")).body?.services ?? [];
  cw = services.find((s: { sessions: number }) => s.sessions >= 3) ?? services[0] ?? null;
  const r = allPairs(dom.taskFactors(), { seed: SEED, constraint: dom.taskConstraint });
  L.writeJson("w2-plan.json", { seed: SEED, agent: agent.name, datasets, cw: cw?.service_name, pairs: r.pairCount, fullProduct: r.fullProductSize, combos: r.combos.length });
});

test.afterAll(async () => {
  L.writeJson("w2-created.json", created);
  await admin.ctx.close();
});

async function sessionsSeen(hours: number): Promise<number> {
  // the observability session list the wizard itself reads (7d), narrowed like the wizard does
  const rows = ((await admin.api.get("/api/observability/sessions?range=7d")).body?.sessions ?? []).filter((s: { agent: string }) => s.agent === FX_AGENT);
  const cutoff = Date.now() - hours * 3_600_000;
  return rows.filter((s: { last: string | null }) => !s.last || new Date(s.last).getTime() >= cutoff).length;
}

interface Drive {
  /** the fixture could not supply the row's picks: the row is BLOCKED, not a finding */
  fixtureGap?: boolean;
  blocked: boolean;
  reasons: string[];
  notes: string[];
  status: number | null;
  body: Record<string, unknown> | null;
  req: Record<string, unknown> | null;
  screenshots: string[];
}

async function drive(page: Page, combo: Combo, name: string, id: string): Promise<Drive> {
  const d: Drive = { blocked: false, reasons: [], notes: [], status: null, body: null, req: null, screenshots: [], fixtureGap: false };
  await L.goto(page, "/v2/eval/tasks?view=new", 300);
  await page.getByTestId("v2-task-name").waitFor({ timeout: 30_000 });
  if (combo.target === "cloudwatch") {
    await page.getByTestId("v2-task-target-cloudwatch").click();
    const use = page.getByTestId("v2-task-cw-use").first();
    // the discovered-services table loads after the target switch
    await use.waitFor({ timeout: 20_000 }).catch(() => undefined);
    if (await use.count()) await use.click();
    else if (cw) {
      await page.getByTestId("v2-task-cw-service").fill(cw.service_name);
      d.notes.push("cw service typed (no 'use' link)");
    }
  }
  await page.getByTestId("v2-task-name").fill(name);
  if (combo.target === "agent") await L.pickOption(page, page.getByTestId("v2-task-agent"), new RegExp(`^${FX_AGENT}$`));
  await page.getByTestId(`v2-task-mode-${combo.mode}`).click();
  const src = page.getByTestId(`v2-task-src-${combo.source}`);
  if (await src.isDisabled()) {
    d.reasons.push(`source ${combo.source} disabled`);
    d.blocked = true;
    return d;
  }
  await src.click();
  if (combo.strategy === "continuous") {
    const c = page.getByTestId("v2-task-strategy-continuous");
    if (await c.isDisabled()) {
      d.reasons.push("continuous disabled");
      d.blocked = true;
      return d;
    }
    await c.click();
    const sampling = page.locator(".v2-form input[type=number]").first();
    await sampling.fill(combo.sampling);
    await sampling.blur();
    d.notes.push(`sampling field reads ${await sampling.inputValue()}`);
  }
  if (combo.window !== "na") await L.pickOption(page, page.getByTestId("v2-task-lookback"), new RegExp(`^Last ${combo.window} hours$`));
  if (combo.source === "dataset") await L.pickOption(page, page.getByTestId("v2-task-dataset"), combo.dataset === "gt" ? /x16n-ds-gt/ : /x16n-ds-sim/);
  if (combo.source === "sessions") {
    const rows = page.locator('input[data-testid^="v2-task-session-"]');
    await rows.first().waitFor({ timeout: 30_000 }).catch(() => d.notes.push("no session rows"));
    if (!(await rows.count())) {
      d.notes.push("FIXTURE: no sessions to pick");
      d.fixtureGap = true;
    } else if (combo.picked === "three") await page.getByTestId("v2-task-sessions-all").click();
    else await rows.first().click();
  }
  if (combo.source === "logs") {
    const rows = page.locator('input[data-testid^="v2-task-log-"]:not([disabled])');
    await rows.first().waitFor({ timeout: 60_000 }).catch(() => d.notes.push("no pickable log-stream rows"));
    if (!(await rows.count())) {
      d.notes.push("FIXTURE: no log streams to pick");
      d.fixtureGap = true;
    } else if (combo.picked === "three") await page.getByTestId("v2-task-logs-all").click();
    else await rows.first().click();
  }
  d.screenshots.push(await L.shot(page, `${id}-step0`));
  await page.getByTestId("v2-task-next").click();
  await page.waitForTimeout(400);
  const alert = page.locator(".v2-alert.error, [class*=alert]").filter({ hasText: /./ }).first();
  const step1 = await page.locator('[data-testid^="v2-task-eval-"], [data-testid^="v2-task-insight-"]').first().isVisible().catch(() => false);
  if (!step1) {
    d.blocked = true;
    d.reasons.push(`step0: ${(await alert.innerText().catch(() => "")).slice(0, 120)}`);
    d.screenshots.push(await L.shot(page, `${id}-blocked0`));
    return d;
  }
  if (combo.mode === "evaluators") {
    const want = combo.evaluators === "one" ? [NON_GT[0]] : combo.evaluators === "two" ? DEFAULT_EVALS : combo.evaluators === "ten" ? NON_GT.slice(0, 10) : combo.evaluators === "gt" ? [...DEFAULT_EVALS, GT] : [];
    for (const e of DEFAULT_EVALS) if (!want.includes(e)) await page.getByTestId(`v2-task-eval-${e}`).uncheck().catch(() => d.notes.push(`uncheck ${e} failed`));
    for (const e of want) {
      const box = page.getByTestId(`v2-task-eval-${e}`);
      if (await box.isDisabled()) d.notes.push(`${e} disabled`);
      else if (!(await box.isChecked())) await box.check();
    }
    // the 11th box must be disabled at the cap
    if (combo.evaluators === "ten") {
      const eleventh = page.getByTestId(`v2-task-eval-${GT}`);
      if (!(await eleventh.isDisabled())) d.notes.push("11th evaluator box not disabled at the cap");
    }
  } else {
    const boxes = page.locator('input[data-testid^="v2-task-insight-"]');
    const n = await boxes.count();
    for (let i = 0; i < n; i += 1) {
      const on = combo.insights === "all" || (combo.insights === "one" && i === 0);
      if ((await boxes.nth(i).isChecked()) !== on) await boxes.nth(i).click();
    }
  }
  const resP = page.waitForResponse((r) => /\/api\/eval\/(runs|online)$/.test(r.url()) && r.request().method() === "POST", { timeout: 60_000 }).catch(() => null);
  await page.getByTestId("v2-task-submit").click();
  const res = await Promise.race([resP, page.waitForTimeout(3000).then(() => null)]);
  if (res) {
    d.status = res.status();
    d.body = await res.json().catch(() => null);
    d.req = (res.request().postDataJSON() as Record<string, unknown>) ?? null;
  } else {
    d.blocked = true;
    const text = await alert.innerText().catch(() => "");
    d.reasons.push(`step1: ${text.slice(0, 120)}`);
  }
  d.screenshots.push(await L.shot(page, `${id}-submit`));
  return d;
}

async function runCombo(page: Page, idx: string, combo: Combo) {
  const id = `${idx}`;
  const env: dom.TaskEnv = { sessionsInWindow: () => 3, gtDatasetHasGroundTruth: datasets.gt.has_ground_truth };
  if (combo.source === "window" && combo.target === "agent" && combo.strategy !== "continuous") env.sessionsInWindow = () => 0; // filled below
  const seen = combo.source === "window" && combo.target === "agent" ? await sessionsSeen(Number(combo.window)) : 3;
  env.sessionsInWindow = () => seen;
  const exp = dom.expectTask(combo, env);
  const name = `x16n-t${idx.replace(/\D/g, "")}-${combo.source}-${combo.mode}`.slice(0, 64);
  const m = L.mark(admin);
  let d: Drive;
  try {
    d = await drive(page, combo, name, id);
  } catch (err) {
    L.record({ suite: SUITE, id, verdict: "BLOCKED", combo, expected: JSON.stringify(exp), observed: `drive error: ${String(err).slice(0, 300)}`, screenshots: [await L.shot(page, `${id}-error`)] });
    return;
  }
  if (d.fixtureGap) {
    L.record({ suite: SUITE, id, verdict: "BLOCKED", combo, expected: JSON.stringify(exp), observed: d.notes.join("; "), screenshots: d.screenshots });
    return;
  }
  const gotUi: dom.Verdict = d.blocked ? "blocked" : "allowed";
  let gotSrv: string = "n/a";
  const gaps: string[] = [];
  if (d.status !== null) {
    gotSrv = d.status < 300 ? "accept" : String(d.status);
    if (d.status < 300 && d.body) {
      const kind = d.req && "sampling_percentage" in d.req ? "online" : "run";
      const rid = String(kind === "online" ? d.body.config_id : d.body.id);
      const keep = created.filter((c) => c.keep).length < KEEP_RUNS;
      created.push({ kind, id: rid, keep });
      // the created row must reflect the choices
      if (kind === "run") {
        const run = (await admin.api.get(`/api/eval/runs/${encodeURIComponent(rid)}`)).body ?? {};
        if (run.mode !== (combo.mode === "insights" ? "insights" : "evaluators")) gaps.push(`mode ${run.mode}`);
        const sentEv = ((d.req?.evaluators as string[]) ?? []).slice().sort();
        const rawEv = combo.mode === "insights" ? (run.insights ?? run.evaluators) : run.evaluators;
        const gotEv = (Array.isArray(rawEv) ? (rawEv as string[]) : []).slice().sort();
        if (combo.mode === "evaluators" && JSON.stringify(sentEv) !== JSON.stringify(gotEv)) gaps.push(`evaluators sent ${sentEv} stored ${gotEv}`);
        if (combo.target === "agent" && run.agent_id !== agent.id) gaps.push(`agent ${run.agent_id}`);
        if (combo.source === "window" && run.dataset_name !== `window:${combo.window}h`) gaps.push(`dataset_name ${run.dataset_name}`);
        if (combo.source === "dataset" && run.dataset_id !== (combo.dataset === "gt" ? datasets.gt.id : datasets.sim.id)) gaps.push(`dataset_id ${run.dataset_id}`);
        if ((combo.source === "sessions" || combo.source === "logs") && (run.session_ids ?? []).length !== (combo.picked === "three" ? 3 : 1) && (run.session_ids ?? []).length < (combo.picked === "three" ? 3 : 1)) gaps.push(`session_ids ${(run.session_ids ?? []).length}`);
        if (!keep) {
          await admin.api.post(`/api/eval/runs/${encodeURIComponent(rid)}/stop`);
          await L.sleep(1500);
          const del = await admin.api.delete(`/api/eval/runs/${encodeURIComponent(rid)}`);
          if (del.status >= 300) created[created.length - 1].keep = true;
        }
      } else {
        const cfg = (await admin.api.get(`/api/eval/online/${encodeURIComponent(rid)}`)).body ?? {};
        if (exp.samplingSent !== undefined && Number(cfg.sampling_percentage) !== exp.samplingSent) gaps.push(`sampling stored ${cfg.sampling_percentage} expected ${exp.samplingSent}`);
        if (!keep) await admin.api.delete(`/api/eval/online/${encodeURIComponent(rid)}`);
      }
    }
  }
  let verdict: L.Result["verdict"] = "PASS";
  let c: L.Cls | undefined;
  let why = "";
  const srvExpected = exp.server === "accept" ? "accept" : String(exp.server);
  if (gotUi === "allowed" && gotSrv !== "accept" && gotSrv !== "n/a") {
    verdict = "FAIL";
    c = exp.server !== "accept" ? "UX" : "INCONSISTENT";
    why = `UI allowed it, server ${gotSrv}${exp.server !== "accept" ? " (oracle: server was expected to refuse — the wizard could have said so)" : ""}`;
  } else if (gotUi !== exp.ui) {
    verdict = "FAIL";
    c = "INCONSISTENT";
    why = `oracle expected UI ${exp.ui}, wizard ${gotUi}`;
  } else if (gotSrv !== "n/a" && gotSrv !== srvExpected) {
    verdict = "FAIL";
    c = "INCONSISTENT";
    why = `server ${gotSrv} vs expected ${srvExpected}`;
  }
  const sinceM = L.since(admin, m);
  if (verdict === "PASS" && (gaps.length || sinceM.pageErrors.length)) {
    verdict = "FAIL";
    c = sinceM.pageErrors.length ? "BROKEN" : "INCONSISTENT";
  }
  const observed = [`ui=${gotUi}${d.reasons.length ? ` (${d.reasons.join("; ")})` : ""}`, `server=${gotSrv}${d.body && d.status && d.status >= 300 ? ` ${JSON.stringify(d.body).slice(0, 200)}` : ""}`, gaps.length ? `gaps: ${gaps.join(", ")}` : "", d.notes.join("; "), sinceM.pageErrors.length ? `pageErrors: ${sinceM.pageErrors.map((e) => e.text).join(" | ")}` : ""].filter(Boolean).join(" · ");
  L.record({ suite: SUITE, id, verdict, cls: verdict === "FAIL" ? c : undefined, combo, expected: `ui=${exp.ui} server=${srvExpected} [${[...exp.uiReasons, ...exp.serverReasons].join(", ")}]`, observed: `${why ? `${why} · ` : ""}${observed}`, screenshots: d.screenshots, repro: { seed: SEED, name, combo: comboLabel(combo), req: d.req } });
  expect.soft(verdict, `${id} ${comboLabel(combo)} :: ${why || observed}`).toBe("PASS");
}

test("task wizard pairwise rows", async () => {
  const r = allPairs(dom.taskFactors(), { seed: SEED, constraint: dom.taskConstraint });
  for (const [i, combo] of r.combos.entries()) {
    if (ONLY_ROWS.length && !ONLY_ROWS.includes(i)) continue;
    await runCombo(admin.page, `T${String(i).padStart(2, "0")}`, combo);
  }
});

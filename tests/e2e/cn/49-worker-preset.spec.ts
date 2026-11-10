import { expect, test } from "@playwright/test";
import { awsRead, ledger, waitForJob } from "./helpers";
import { WORKER_PRESET_KEY, WORKER_RUNTIME_ID, campaignDataset, pollUntil, record, smokeAgent, snap } from "./n12e";

/**
 * Plan §E.3 flow 11 — the evaluation-worker preset. RUNS LAST (it rebuilds the worker runtime).
 * Agents → System presets shows `eval-worker` INSTALLED (version pinned); REPAIR
 * (`POST /api/system-agents/eval-worker/install` with an empty body = "repair with the stored
 * choices") redeploys it — a new Runtime version; UNINSTALL → a new run answers 503
 * `evaluation.worker_missing` while datasets / evaluators / SCORE NOW still work; reinstall.
 * The task detail of flow 3 showed the worker's progress messages (ledger `case26 … |replaying…`).
 */
async function preset(api: import("@playwright/test").APIRequestContext): Promise<Record<string, unknown>> {
  const body = await (await api.get("/api/system-agents")).json();
  return (body.presets as Array<Record<string, unknown>>).find((p) => p.key === WORKER_PRESET_KEY)!;
}

function runtimeVersion(runtimeId: string = WORKER_RUNTIME_ID): { status: string; version: string } {
  try {
    const j = JSON.parse(awsRead(`bedrock-agentcore-control get-agent-runtime --agent-runtime-id ${runtimeId}`));
    return { status: String(j.status), version: String(j.agentRuntimeVersion) };
  } catch {
    return { status: "?", version: "?" };
  }
}

test("case 32 (flow 11) — worker preset: INSTALLED → REPAIR redeploys → UNINSTALL → 503 worker_missing → reinstall", async ({ page }, testInfo) => {
  test.setTimeout(60 * 60 * 1000);
  const t0 = Date.now();
  const api = page.request;
  let p = await preset(api);
  await record(testInfo, "preset-before.json", p, `status=${p.status} installed=${p.installed_skill_version} can_repair=${p.can_repair} agent=${p.agent_id}`);
  expect(String(p.status)).toBe("active");
  // the worker runtime id changes with every reinstall — resolve it from the preset's agent row
  const workerAgent = (await (await api.get(`/api/agents/${p.agent_id}`)).json()) as { resource_id?: string };
  const runtimeId = String(workerAgent.resource_id ?? WORKER_RUNTIME_ID);
  ledger(`case32 worker runtime id ${runtimeId}`);
  const v0 = runtimeVersion(runtimeId);
  ledger(`case32 worker runtime before: status=${v0.status} version=${v0.version}`);

  // the /agents/new SYSTEM PRESETS panel — still the presets surface after r13-a retired the
  // classic console (no V2 twin yet); the V2 agents list shows the SYSTEM chip
  await page.goto("/agents/new");
  await expect(page.getByText("Evaluation worker").first()).toBeVisible({ timeout: 60_000 });
  const panel = await page.locator("body").innerText();
  const repairOffered = /REPAIR/.test(panel);
  ledger(`case32 presets panel: ACTIVE shown=${/SYSTEM ACTIVE/.test(panel)} skill v${p.skill_version} installed v${p.installed_skill_version}; REPAIR button offered=${repairOffered}; UNINSTALL offered=${/UNINSTALL/.test(panel)}`);
  await snap(page, "case32-presets-installed", `REPAIR offered=${repairOffered}`);
  await page.goto("/v2/agents");
  await snap(page, "case32-v2-agents-system-chip");

  // REPAIR
  const tRepair = Date.now();
  let repair = await api.post(`/api/system-agents/${WORKER_PRESET_KEY}/install`, { data: {} });
  let repairBody = await repair.json().catch(async () => ({ raw: await repair.text() }));
  const previousJob = String(p.job_id ?? "");
  ledger(`case32 REPAIR install {} → ${repair.status()} job_id=${repairBody.job_id} (previous ${previousJob}) changed=${repairBody.changed} ${JSON.stringify(repairBody).slice(0, 160)}`);
  if (!repairBody.job_id || String(repairBody.job_id) === previousJob) {
    // an up-to-date preset makes the bodiless repair a no-op (idempotent); `force` redeploys anyway
    ledger("case32 bodiless REPAIR was a no-op (stored spec == desired spec) — forcing a redeploy with {force:true}");
    repair = await api.post(`/api/system-agents/${WORKER_PRESET_KEY}/install`, { data: { force: true } });
    repairBody = await repair.json().catch(async () => ({ raw: await repair.text() }));
    ledger(`case32 REPAIR install {force:true} → ${repair.status()} job_id=${repairBody.job_id} ${JSON.stringify(repairBody).slice(0, 160)}`);
  }
  await record(testInfo, "repair-response.json", repairBody);
  expect(repair.status(), JSON.stringify(repairBody)).toBeLessThan(300);
  expect(repairBody.job_id, "repair launched a deploy job").toBeTruthy();
  const { body: afterRepair } = await pollUntil<{ presets: Array<Record<string, unknown>> }>(
    api,
    "/api/system-agents",
    (b) => {
      const row = b.presets.find((x) => x.key === WORKER_PRESET_KEY)!;
      return !row.operation && ["active", "failed"].includes(String(row.status)) && row.deployment_status !== "running";
    },
    { timeoutMs: 20 * 60 * 1000, intervalMs: 15_000, label: "case32 repair", key: (b) => { const r = b.presets.find((x) => x.key === WORKER_PRESET_KEY)!; return `${r.status} op=${r.operation ?? "-"} dep=${r.deployment_status} job=${String(r.job_id).slice(0, 8)}`; } },
  );
  p = afterRepair.presets.find((x) => x.key === WORKER_PRESET_KEY)!;
  const repairSeconds = Math.round((Date.now() - tRepair) / 1000);
  const v1 = runtimeVersion(runtimeId);
  await record(testInfo, "preset-after-repair.json", p, `repair ${repairSeconds}s status=${p.status} dep=${p.deployment_status}; runtime ${v0.version} → ${v1.version} (${v1.status})`);
  await page.goto(`/v2/agents?view=detail&id=${p.agent_id}`);
  await snap(page, "case32-worker-after-repair", `v${v1.version} in ${repairSeconds}s`);
  expect(String(p.status)).toBe("active");
  expect(String(p.deployment_status)).toBe("succeeded");
  expect(Number(v1.version), "REPAIR minted a new runtime version").toBeGreaterThan(Number(v0.version));

  // the repaired worker still runs a job: a quick batch run (3 scenarios) must complete
  const agent = await smokeAgent(api);
  const ds = await campaignDataset(api);
  const probeRun = await api.post("/api/eval/runs", { data: { agent_id: agent.id, dataset_id: ds.id, evaluators: ["Builtin.Helpfulness"], name: "n12e-post-repair", wait_seconds: 60 } });
  const probeBody = await probeRun.json();
  ledger(`case32 post-repair run → ${probeRun.status()} id=${probeBody.id} status=${probeBody.status}`);
  expect(probeRun.status()).toBe(201);

  // UNINSTALL → worker_missing
  const tUn = Date.now();
  const un = await api.delete(`/api/system-agents/${WORKER_PRESET_KEY}`);
  const unBody = await un.json().catch(async () => ({ raw: await un.text() }));
  ledger(`case32 UNINSTALL → ${un.status()} ${JSON.stringify(unBody).slice(0, 240)}`);
  await record(testInfo, "uninstall-response.json", unBody);
  expect(un.status(), JSON.stringify(unBody)).toBeLessThan(300);
  // the running probe run should settle (completed, or failed because its worker went away) — record either way
  const probe = await pollUntil<Record<string, unknown>>(api, `/api/eval/runs/${probeBody.id}`, (b) => !["queued", "running", "invoking", "waiting", "evaluating"].includes(String(b.status)), { timeoutMs: 15 * 60 * 1000, intervalMs: 15_000, label: "case32 post-repair run", key: (b) => `${b.status} ${String(b.progress ?? "").slice(0, 80)}` });
  ledger(`case32 post-repair run ${probeBody.id} ended ${probe.body.status} (${probe.body.error ?? ""}) — uninstall was issued while it ran`);
  const { body: afterUn } = await pollUntil<{ presets: Array<Record<string, unknown>> }>(
    api,
    "/api/system-agents",
    (b) => { const r = b.presets.find((x) => x.key === WORKER_PRESET_KEY)!; return ["not_installed", "failed"].includes(String(r.status)) && !r.operation; },
    { timeoutMs: 15 * 60 * 1000, intervalMs: 15_000, label: "case32 uninstall", key: (b) => { const r = b.presets.find((x) => x.key === WORKER_PRESET_KEY)!; return `${r.status} op=${r.operation ?? "-"} err=${String(r.error ?? "").slice(0, 100)}`; } },
  );
  p = afterUn.presets.find((x) => x.key === WORKER_PRESET_KEY)!;
  ledger(`case32 uninstall settled in ${Math.round((Date.now() - tUn) / 1000)}s status=${p.status} error=${p.error ?? ""}`);
  await record(testInfo, "preset-after-uninstall.json", p);
  const runtimes = awsRead("bedrock-agentcore-control list-agent-runtimes");
  ledger(`case32 worker runtime listed after uninstall = ${runtimes.includes(runtimeId)}`);
  expect(String(p.status), `uninstall ended ${p.status}: ${p.error ?? ""}`).toBe("not_installed");

  const missing = await api.post("/api/eval/runs", { data: { agent_id: agent.id, dataset_id: ds.id, evaluators: ["Builtin.Helpfulness"], name: "n12e-no-worker", wait_seconds: 60 } });
  const missingBody = await missing.text();
  ledger(`case32 run without worker → ${missing.status()} ${missingBody.slice(0, 200)}`);
  expect(missing.status()).toBe(503);
  expect(missingBody).toContain("evaluation.worker_missing");
  // datasets / evaluators / SCORE NOW keep working
  expect((await api.get("/api/eval/datasets")).ok()).toBeTruthy();
  expect((await api.get("/api/eval/evaluators")).ok()).toBeTruthy();
  const sessions = (await (await api.get(`/api/chat/${agent.id}/sessions`)).json()).sessions as Array<{ session_id: string }>;
  const score = await api.post(`/api/observability/sessions/${sessions[sessions.length - 1].session_id}/evaluate`, { data: { evaluator_ids: ["Builtin.Coherence"], range: "24h" }, timeout: 5 * 60 * 1000 });
  ledger(`case32 SCORE NOW without worker → ${score.status()}`);
  expect(score.ok()).toBeTruthy();
  await page.goto("/v2/eval/tasks?view=new");
  await snap(page, "case32-tasks-without-worker");
  await page.goto("/agents/new");
  await snap(page, "case32-presets-uninstalled");

  // reinstall
  const tRe = Date.now();
  const re = await api.post(`/api/system-agents/${WORKER_PRESET_KEY}/install`, { data: {} });
  const reBody = await re.json().catch(async () => ({ raw: await re.text() }));
  ledger(`case32 reinstall → ${re.status()} ${JSON.stringify(reBody).slice(0, 240)}`);
  expect(re.status(), JSON.stringify(reBody)).toBeLessThan(300);
  if (reBody.job_id) {
    const job = await waitForJob(api, String(reBody.job_id), { timeoutMs: 20 * 60 * 1000 }).catch((e: Error) => ({ job_id: reBody.job_id, status: `unreadable (${e.message.slice(0, 60)})` }));
    ledger(`case32 reinstall job ${String(reBody.job_id).slice(0, 8)} → ${job.status}`);
  }
  const { body: afterRe } = await pollUntil<{ presets: Array<Record<string, unknown>> }>(
    api,
    "/api/system-agents",
    (b) => { const r = b.presets.find((x) => x.key === WORKER_PRESET_KEY)!; return ["active", "failed"].includes(String(r.status)) && !r.operation && r.deployment_status !== "running"; },
    { timeoutMs: 20 * 60 * 1000, intervalMs: 15_000, label: "case32 reinstall", key: (b) => { const r = b.presets.find((x) => x.key === WORKER_PRESET_KEY)!; return `${r.status} op=${r.operation ?? "-"} dep=${r.deployment_status}`; } },
  );
  p = afterRe.presets.find((x) => x.key === WORKER_PRESET_KEY)!;
  const reSeconds = Math.round((Date.now() - tRe) / 1000);
  await record(testInfo, "preset-after-reinstall.json", p, `reinstall ${reSeconds}s status=${p.status} agent=${p.agent_id}`);
  const runtimesAfter = awsRead("bedrock-agentcore-control list-agent-runtimes");
  const newRuntime = /launchpad_eval_worker_t1[^"]*-[A-Za-z0-9]{10}/.exec(runtimesAfter)?.[0];
  ledger(`case32 worker runtime after reinstall: ${newRuntime ?? "(none)"}`);
  await page.goto("/agents/new");
  await snap(page, "case32-presets-reinstalled", `${p.status} in ${reSeconds}s`);
  expect(String(p.status), `reinstall ended ${p.status}: ${p.error ?? ""}`).toBe("active");
  ledger(`case32 PASS repair ${repairSeconds}s, reinstall ${reSeconds}s, total ${Math.round((Date.now() - t0) / 60) / 1000} min`);
});

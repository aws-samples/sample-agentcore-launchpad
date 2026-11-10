import { expect, test, type APIRequestContext } from "@playwright/test";
import { awsRead, chatOnce, getAgent, ledger, waitForJob } from "./helpers";
import { createCanaryViaUi, expectConfigurationRedirect } from "./canary-ui";
import { campaignDataset, pollUntil, record, runtimeEndpoints, smokeAgent, snap } from "./n12e";

/**
 * §4.1 case 31 / plan §E.3 flows 8 + 9 — Experiment A/B and Runtime canary.
 *
 * N12-obs49 (user decision 2026-10-16) made the V2 Experiments module canary-only; N13-obs2 (user decision
 * 2026-10-17) retired the configuration-bundle experiment (flow 8) altogether — pages, `/api/experiments`
 * routes and all. Case 31a now only asserts what is left of it: the deep-link redirect to the canary list and
 * the 404s. The runtime canary (flow 9) is created through the CanaryStart form — champion, candidate prompt,
 * evaluators + primary (N12-obs46) — with hard expectations (`cn/canary-ui.ts`): no API fallback hides a UI
 * regression.
 *
 * Flow 9: canary START with an edited prompt → setup creates `stable…` / `treat…` endpoints
 * (read-only `list-agent-runtime-endpoints`, Agents → Versions) → traffic → verdict → advance
 * (50/50) → complete (or rollback) → cleanup removes the endpoints.
 */
const ACTION_TIMEOUT = 25 * 60 * 1000;

async function act(api: APIRequestContext, path: string, body: Record<string, unknown>, label: string): Promise<{ status: number; body: Record<string, unknown> }> {
  const res = await api.post(path, { data: body, timeout: 120_000 });
  const text = await res.text();
  let parsed: Record<string, unknown> = {};
  try { parsed = JSON.parse(text); } catch { parsed = { raw: text }; }
  ledger(`${label} POST ${path} ${JSON.stringify(body).slice(0, 120)} → ${res.status()} ${text.slice(0, 200).replace(/\n/g, " ")}`);
  return { status: res.status(), body: parsed };
}

/** Wait until `running_action` clears and the stage/artifact `key` is present (or status failed). */
async function settle(api: APIRequestContext, path: string, key: string, label: string, timeoutMs = ACTION_TIMEOUT) {
  return pollUntil<Record<string, unknown>>(
    api,
    path,
    (b) => !b.running_action && ((b.artifacts as Record<string, unknown> | undefined)?.[key] != null || String(b.status).match(/failed|error/i) != null),
    { timeoutMs, intervalMs: 10_000, label, key: (b) => `${b.status}/${b.stage} running=${b.running_action ?? "-"} progress=${String(b.progress ?? "").slice(0, 100)}` },
  );
}

function spansFor(sessionId: string, sinceMs: number): string {
  // read-only CloudWatch Logs read over the aws/spans group of this account
  return awsRead(`logs filter-log-events --log-group-name aws/spans --start-time ${sinceMs} --filter-pattern "${sessionId}" --limit 200`);
}

test("case 31a — configuration experiment retired (N13-obs2): deep links land on the canary list, the API is gone", async ({ page }, testInfo) => {
  test.setTimeout(5 * 60 * 1000);
  const api = page.request;
  // N12-obs49 hid the configuration experiment; N13-obs2 (user decision 2026-10-17) deleted it. The
  // redirect rule is the only thing left of it in the console — assert it still holds…
  await expectConfigurationRedirect(page, "case31");
  await snap(page, "case31-configuration-redirect", "?mode=configuration → canary list (N12-obs49 / N13-obs2)");
  // …and the backend answers 404 on the retired routes (no 405, no 5xx: the router is unmounted, not disabled)
  for (const path of ["/api/experiments", "/api/experiments/readiness?agent_id=x", "/api/experiments/providers"]) {
    const res = await api.get(path, { timeout: 60_000 });
    ledger(`case31 GET ${path} → ${res.status()}`);
    expect(res.status(), `${path} is retired`).toBe(404);
  }
  const created = await act(api, "/api/experiments", { agent_id: "x", lookback_hours: 24 }, "case31 create (retired)");
  expect(created.status, "POST /api/experiments is retired").toBe(404);
  // the recommendation-provider discovery the run-recommendation picker needs moved with the retirement
  const providers = await (await api.get("/api/eval/recommendation-providers", { timeout: 60_000 })).json() as { providers: Array<{ id: string; enabled?: boolean }> };
  await record(testInfo, "recommendation-providers.json", providers, `providers=${providers.providers.map((p) => p.id).join(",")}`);
  expect(providers.providers.map((p) => p.id)).toContain("gepa_lite");
  ledger("case31a PASS configuration experiment retired (N13-obs2)");
});

test("case 31b — runtime canary: setup endpoints → traffic → verdict → advance → complete → cleanup", async ({ page }, testInfo) => {
  test.setTimeout(90 * 60 * 1000);
  const t0 = Date.now();
  const api = page.request;
  const agent = await smokeAgent(api);
  const ds = await campaignDataset(api);
  const before = await getAgent(api, agent.id);
  const basePrompt = String((before.spec as Record<string, unknown>).system_prompt ?? "");
  const endpointsBefore = runtimeEndpoints(agent.resource_id);
  ledger(`case31b endpoints before: ${endpointsBefore.map((e) => `${e.name}=${e.status}/v${e.liveVersion}`).join(", ")}`);

  const candidatePrompt = basePrompt + "\nAlways end your reply with the word 'Cheers!'.";
  // N12-obs46 / obs49: CanaryStart (champion → candidate prompt → evaluators + primary → CREATE → detail), hard — no API fallback
  const created = { status: 201, body: await createCanaryViaUi(page, api, { agentId: agent.id, agentName: agent.name, candidatePrompt, label: "case31b", onForm: (p) => snap(p, "case31-canary-new-form", "champion + candidate prompt + evaluators/primary") }) };
  expect(created.status, JSON.stringify(created.body)).toBe(201);
  const id = String(created.body.id);
  const path = `/api/runtime-canaries/${id}`;
  const detailUrl = `/v2/eval/experiments?mode=canary&canary=${id}`;

  const tSetup = Date.now();
  const setup = await act(api, `${path}/action`, { action: "setup" }, "case31b setup");
  expect(setup.status).toBeLessThan(300);
  // mid-setup: the stable endpoint appears first (PARTIAL setup pins every session to stable)
  await page.waitForTimeout(45_000);
  const mid = runtimeEndpoints(agent.resource_id);
  ledger(`case31b endpoints 45s into setup: ${mid.map((e) => `${e.name}=${e.status}/v${e.liveVersion}`).join(", ")}`);
  let row = (await settle(api, path, "abtest", "case31b setup", 30 * 60 * 1000)).body;
  const setupArt = ((row.artifacts as Record<string, unknown>).setup ?? {}) as Record<string, unknown>;
  const setupSeconds = Math.round((Date.now() - tSetup) / 1000);
  await record(testInfo, "canary-setup.json", row.artifacts, `setup ${setupSeconds}s stable=${setupArt.stable_endpoint} treat=${setupArt.treatment_endpoint}`);
  const eps = runtimeEndpoints(agent.resource_id);
  ledger(`case31b endpoints after setup (${setupSeconds}s): ${eps.map((e) => `${e.name}=${e.status}/v${e.liveVersion}`).join(", ")}`);
  await record(testInfo, "endpoints-after-setup.json", eps);
  await page.goto(detailUrl);
  await page.waitForTimeout(2000);
  await snap(page, "case31-canary-setup", eps.map((e) => e.name).join(","));
  await page.goto(`/v2/agents?view=detail&id=${agent.id}`);
  await snap(page, "case31-agent-versions-canary-endpoints");
  expect(String(row.status)).not.toMatch(/failed/i);
  expect(eps.some((e) => e.name.startsWith("stable")), "stable… endpoint exists on AWS").toBeTruthy();
  expect(eps.some((e) => e.name.startsWith("treat")), "treat… endpoint exists on AWS").toBeTruthy();
  const weights0 = ((row.artifacts as Record<string, unknown>).abtest as Record<string, unknown>) ?? {};
  ledger(`case31b abtest after setup: ${JSON.stringify(weights0).slice(0, 300)}`);

  const tTraffic = Date.now();
  const traffic = await act(api, `${path}/action`, { action: "traffic", dataset_id: ds.id }, "case31b traffic");
  expect(traffic.status).toBeLessThan(300);
  row = (await settle(api, path, "rounds", "case31b traffic")).body;
  ledger(`case31b traffic settled in ${Math.round((Date.now() - tTraffic) / 1000)}s rounds=${JSON.stringify((row.artifacts as Record<string, unknown>).rounds).slice(0, 300)}`);
  await page.goto(detailUrl);
  await snap(page, "case31-canary-traffic");

  const verdict = await act(api, `${path}/action`, { action: "verdict" }, "case31b verdict");
  expect(verdict.status).toBeLessThan(300);
  row = (await pollUntil<Record<string, unknown>>(api, path, (b) => !b.running_action, { timeoutMs: 10 * 60 * 1000, label: "case31b verdict", key: (b) => `${b.stage} ${b.running_action ?? ""}` })).body;
  const rounds = ((row.artifacts as Record<string, unknown>).rounds ?? []) as Array<Record<string, unknown>>;
  const lastVerdict = rounds.length ? (rounds[rounds.length - 1].verdict ?? rounds[rounds.length - 1]) : null;
  await record(testInfo, "canary-verdict.json", rounds, `verdict ${JSON.stringify(lastVerdict).slice(0, 300)}`);
  await page.goto(detailUrl);
  await snap(page, "case31-canary-verdict");

  const advance = await act(api, `${path}/action`, { action: "advance", allow_non_significant: true }, "case31b advance");
  expect(advance.status, "advance accepted (allow_non_significant)").toBeLessThan(300);
  row = (await pollUntil<Record<string, unknown>>(api, path, (b) => !b.running_action, { timeoutMs: 10 * 60 * 1000, label: "case31b advance" })).body;
  const ab1 = (row.artifacts as Record<string, unknown>).abtest as Record<string, unknown>;
  ledger(`case31b after advance: ${JSON.stringify(ab1).slice(0, 300)}`);
  await page.goto(detailUrl);
  await snap(page, "case31-canary-advanced-50-50");
  expect(JSON.stringify(ab1)).toMatch(/50/);

  // complete needs the last ramp stage — advance once more (1/99) then complete; fall back to rollback
  let completed = false;
  const advance2 = await act(api, `${path}/action`, { action: "advance", allow_non_significant: true }, "case31b advance2");
  if (advance2.status < 300) {
    row = (await pollUntil<Record<string, unknown>>(api, path, (b) => !b.running_action, { timeoutMs: 10 * 60 * 1000, label: "case31b advance2" })).body;
    const complete = await act(api, `${path}/action`, { action: "complete", allow_non_significant: true }, "case31b complete");
    if (complete.status < 300) {
      row = (await settle(api, path, "complete", "case31b complete", 20 * 60 * 1000)).body;
      completed = true;
    }
  }
  if (!completed) {
    const rb = await act(api, `${path}/action`, { action: "rollback" }, "case31b rollback");
    expect(rb.status).toBeLessThan(300);
    row = (await settle(api, path, "rollback", "case31b rollback", 20 * 60 * 1000)).body;
  }
  const after = await getAgent(api, agent.id);
  ledger(`case31b ${completed ? "complete" : "rollback"} → status=${row.status} stage=${row.stage}; agent version ${before.version} → ${after.version}; prompt has Cheers=${String((after.spec as Record<string, unknown>).system_prompt).includes("Cheers")}`);
  await page.goto(detailUrl);
  await snap(page, completed ? "case31-canary-complete" : "case31-canary-rollback");

  const cleanup = await act(api, `${path}/action`, { action: "cleanup" }, "case31b cleanup");
  expect(cleanup.status).toBeLessThan(300);
  row = (await settle(api, path, "cleanup", "case31b cleanup", 10 * 60 * 1000)).body;
  const epsAfter = runtimeEndpoints(agent.resource_id);
  ledger(`case31b endpoints after cleanup: ${epsAfter.map((e) => `${e.name}=${e.status}`).join(", ") || "(none listed)"}`);
  await record(testInfo, "endpoints-after-cleanup.json", epsAfter);
  await page.goto(detailUrl);
  await snap(page, "case31-canary-cleanup");
  expect(epsAfter.some((e) => e.name.startsWith("stable") || e.name.startsWith("treat")), "canary endpoints removed").toBeFalsy();
  ledger(`case31b PASS canary ${id} in ${Math.round((Date.now() - t0) / 60) / 1000} min`);
});

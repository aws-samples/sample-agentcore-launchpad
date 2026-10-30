import { expect, test, type APIRequestContext } from "@playwright/test";
import { awsRead, ledger, listAgents, sleep } from "./helpers";
import { N12E_EVALUATOR_PREFIX, N12E_PREFIX, isN12E, record } from "./n12e";

/**
 * N12 campaign cleanup — through the product only, `n12e-` / `n12e_` rows only, in the order
 * canaries → tasks → online evals → datasets (cloud snapshots first) → evaluators →
 * agents. Never touches the eval-worker / architect presets or anything else. Verifies with
 * read-only AWS calls that no `n12e` runtime remains. Set N12E_KEEP_AGENT=1 to keep the smoke agent.
 */
interface Done { kind: string; id: string; name?: string; status: number | string }

async function del(api: APIRequestContext, kind: string, path: string, id: string, name: string | undefined, out: Done[]): Promise<void> {
  const res = await api.delete(path);
  out.push({ kind, id, name, status: res.status() });
  ledger(`cleanup ${kind} ${name ?? ""} ${id} DELETE ${path} → ${res.status()} ${(await res.text()).slice(0, 120)}`);
}

async function settleOwner(api: APIRequestContext, path: string): Promise<void> {
  for (let i = 0; i < 40; i++) {
    const row = await (await api.get(path)).json();
    if (!row.running_action) return;
    await sleep(10_000);
  }
}

test("98 — cleanup every n12e-… resource through the product API", async ({ page }, testInfo) => {
  test.setTimeout(40 * 60 * 1000);
  const api = page.request;
  const done: Done[] = [];
  const agentIds = new Set((await listAgents(api)).filter((a) => isN12E(a.name)).map((a) => String(a.id)));

  // (configuration experiments retired — N13-obs2: nothing to clean up, the routes are gone)
  // canaries: a still-running canary is rolled back first (DEFAULT back on the production
  // version), then cleanup deletes the named endpoints and the per-arm online configs
  for (const c of ((await (await api.get("/api/runtime-canaries")).json()).canaries as Array<Record<string, unknown>>)) {
    if (!agentIds.has(String(c.champion_agent_id))) continue;
    if (!(c.artifacts as Record<string, unknown>)?.cleanup) {
      await settleOwner(api, `/api/runtime-canaries/${c.id}`);
      if (c.status === "running") {
        const rb = await api.post(`/api/runtime-canaries/${c.id}/action`, { data: { action: "rollback" } });
        ledger(`cleanup canary ${c.id} rollback → ${rb.status()} ${(await rb.text()).slice(0, 120)}`);
        await settleOwner(api, `/api/runtime-canaries/${c.id}`);
        done.push({ kind: "canary-rollback", id: String(c.id), status: rb.status() });
      }
      const res = await api.post(`/api/runtime-canaries/${c.id}/action`, { data: { action: "cleanup" } });
      ledger(`cleanup canary ${c.id} cleanup → ${res.status()} ${(await res.text()).slice(0, 120)}`);
      await settleOwner(api, `/api/runtime-canaries/${c.id}`);
      done.push({ kind: "canary-cleanup", id: String(c.id), status: res.status() });
    }
  }
  // runs — stop live ones, then delete the deletable (failed / stopped) rows; completed rows have no delete route
  for (const r of ((await (await api.get("/api/eval/runs?limit=200")).json()).runs as Array<Record<string, unknown>>)) {
    if (!agentIds.has(String(r.agent_id))) continue;
    if (["queued", "running", "invoking", "waiting", "evaluating"].includes(String(r.status))) {
      const stop = await api.post(`/api/eval/runs/${r.id}/stop`);
      ledger(`cleanup run ${r.id} stop → ${stop.status()}`);
      await sleep(5000);
    }
    const res = await api.delete(`/api/eval/runs/${r.id}`);
    done.push({ kind: "run", id: String(r.id), name: String(r.name ?? ""), status: res.status() });
    ledger(`cleanup run ${r.id} (${r.status}) DELETE → ${res.status()}`);
  }
  // online configs
  for (const c of ((await (await api.get("/api/eval/online")).json()).configs as Array<Record<string, unknown>>)) {
    if (agentIds.has(String(c.agent_id)) || isN12E(c.description)) await del(api, "online", `/api/eval/online/${c.config_id}`, String(c.config_id), String(c.description ?? ""), done);
  }
  // datasets: cloud snapshots, then local rows
  for (const d of ((await (await api.get("/api/eval/datasets/cloud")).json()).datasets as Array<Record<string, unknown>>)) {
    const name = String(d.name ?? "");
    if (name.startsWith("n12e_") || name.startsWith(N12E_PREFIX)) await del(api, "cloud-dataset", `/api/eval/datasets/cloud/${d.datasetId ?? d.id}`, String(d.datasetId ?? d.id), name, done);
  }
  for (const d of ((await (await api.get("/api/eval/datasets")).json()).datasets as Array<Record<string, unknown>>)) {
    if (isN12E(d.name)) await del(api, "dataset", `/api/eval/datasets/${d.id}`, String(d.id), String(d.name), done);
  }
  // evaluators
  for (const e of ((await (await api.get("/api/eval/evaluators")).json()).evaluators as Array<Record<string, unknown>>)) {
    if (String(e.name ?? "").startsWith(N12E_EVALUATOR_PREFIX)) await del(api, "evaluator", `/api/eval/evaluators/${e.id}`, String(e.id), String(e.name), done);
  }
  // agents (runtime + role + log groups + artifacts go with them)
  if (!process.env.N12E_KEEP_AGENT) {
    for (const a of (await listAgents(api)).filter((x) => isN12E(x.name))) {
      expect(a.system_key ?? null, "never a system preset").toBeNull();
      await del(api, "agent", `/api/agents/${a.id}`, String(a.id), String(a.name), done);
    }
  }

  await sleep(15_000);
  const left = {
    agents: (await listAgents(api)).filter((a) => isN12E(a.name)).map((a) => `${a.name}:${a.status}`),
    datasets: ((await (await api.get("/api/eval/datasets")).json()).datasets as Array<{ name: string }>).filter((d) => isN12E(d.name)).map((d) => d.name),
    evaluators: ((await (await api.get("/api/eval/evaluators")).json()).evaluators as Array<{ name?: string }>).filter((e) => String(e.name ?? "").startsWith(N12E_EVALUATOR_PREFIX)).map((e) => e.name),
    online: ((await (await api.get("/api/eval/online")).json()).configs as Array<{ description?: string }>).filter((c) => isN12E(c.description)).map((c) => c.description),
    runs: ((await (await api.get("/api/eval/runs?limit=200")).json()).runs as Array<{ id: string; status: string; agent_id: string }>).filter((r) => agentIds.has(r.agent_id)).map((r) => `${r.id}:${r.status}`),
  };
  const runtimes = awsRead("bedrock-agentcore-control list-agent-runtimes");
  const n12eRuntimes = (runtimes.match(/"agentRuntimeName":\s*"n12e[^"]*"/g) ?? []).map((s) => s.split('"')[3]);
  await record(testInfo, "cleanup-summary.json", { deleted: done, left, runtimes_left: n12eRuntimes }, `deleted ${done.length} objects; left ${JSON.stringify(left)}; n12e runtimes left ${n12eRuntimes.length}`);
  expect(left.agents.filter((a) => !process.env.N12E_KEEP_AGENT), "no n12e agents left").toEqual([]);
  expect(left.datasets, "no n12e datasets left").toEqual([]);
  expect(left.evaluators, "no n12e evaluators left").toEqual([]);
  expect(left.online, "no n12e online configs left").toEqual([]);
  expect(n12eRuntimes, "no n12e runtime on AWS (read-only list-agent-runtimes)").toEqual([]);
  ledger(`cleanup done: ${done.length} deletions; completed runs left in the ledger (no delete route): ${left.runs.length}`);
});

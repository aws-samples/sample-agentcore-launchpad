import { expect, test, type APIRequestContext } from "@playwright/test";
import { awsRead, ledger, listAgents, registryRecords, sleep } from "../helpers";
import { exportRuntimeLogs } from "../log-export";
import { isScn, isScnEvaluator, isScnPolicy, record, runtimeIdOf, SCN_PREFIX, stateAll, stateGet } from "./scn";

/**
 * Tear down everything the storyline created — through the product only, `e2es-` / `e2es_`
 * rows only — in reverse dependency order:
 *   model providers (D21 s12: the `e2es-` providers and their rows — defaults handed back to the
 *   Bedrock rows first, then DELETE rows → provider; tolerated 403/404 when the registry is absent) →
 *   policies (D20 s11: mode back to `log_only` if a run died in `enforce`, retire + delete the
 *   `e2es_` rows — first, so nothing below is gated) →
 *   canaries (rollback if running, cleanup) → runs (stop, delete the
 *   deletable) → online configs → datasets (cloud snapshots, then local) → evaluators →
 *   agents (HR assistant → runtime twin → payroll specialist; a consumer before its A2A
 *   provider) → knowledge bases → orphan registry records → Skill Lab jobs of this run +
 *   e2es task sets → the member account.
 * Never: system presets (`eval-worker`, Skill Lab worker, architect), the platform-seeded skill
 * records (`expense-report-writer` keeps the version s03 published — product-intended), the
 * bootstrap gateway records. Ends with read-only AWS checks: no `e2es` runtime, role or log group.
 */
interface Done { kind: string; id: string; name?: string; status: number | string }

async function del(api: APIRequestContext, kind: string, url: string, id: string, name: string | undefined, out: Done[]): Promise<void> {
  const res = await api.delete(url);
  out.push({ kind, id, name, status: res.status() });
  ledger(`cleanup ${kind} ${name ?? ""} ${id} DELETE ${url} → ${res.status()} ${(await res.text()).slice(0, 120)}`);
}

/** `e2es-<thing>-<MMDDHHmm>` → ISO start of that minute (this year, or last year if the stamp lies in the future); undefined when no stamp is found. */
function stampStart(names: string[]): string | undefined {
  const stamps = names.map((n) => /-(\d{8})$/.exec(n)?.[1]).filter((s): s is string => !!s).sort();
  if (!stamps.length) return undefined;
  const s = stamps[0];
  const now = new Date();
  let d = new Date(Date.UTC(now.getUTCFullYear(), Number(s.slice(0, 2)) - 1, Number(s.slice(2, 4)), Number(s.slice(4, 6)), Number(s.slice(6, 8))));
  if (d.getTime() > now.getTime()) d = new Date(Date.UTC(now.getUTCFullYear() - 1, Number(s.slice(0, 2)) - 1, Number(s.slice(2, 4)), Number(s.slice(4, 6)), Number(s.slice(6, 8))));
  return d.toISOString();
}

async function settleOwner(api: APIRequestContext, url: string): Promise<void> {
  for (let i = 0; i < 40; i++) {
    const res = await api.get(url, { timeout: 60_000 }).catch(() => null);
    const row = res ? ((await res.json().catch(() => ({}))) as { running_action?: unknown }) : { running_action: "unknown" };
    if (!row.running_action) return;
    await sleep(10_000);
  }
}

test("scenario cleanup — every e2es- / e2es_ object removed in reverse order; read-only AWS shows no e2es runtime / role / log group", async ({ page }, testInfo) => {
  test.setTimeout(60 * 60 * 1000);
  const api = page.request;
  const done: Done[] = [];
  const state = stateAll();
  const agents = (await listAgents(api)).filter((a) => isScn(a.name));
  const agentIds = new Set(agents.map((a) => String(a.id)));
  ledger(`cleanup start agents=${agents.map((a) => a.name).join(",")} state keys=${Object.keys(state).join(",")}`);

  // 0 model providers (D21 s12) — the `e2es-` providers and their rows (admin-only; tolerate 403/404).
  // A row that became a default while a run died mid-s12f/s12c is handed back to the Bedrock row first
  // (a default row cannot be deleted); the provider delete cascades the remaining rows and flags.
  const MP = "/api/settings/model-providers";
  let registryLeft: string[] = [];
  const mpRes = await api.get(MP, { timeout: 60_000 }).catch(() => null);
  if (mpRes?.ok()) {
    const reg = (await mpRes.json()) as { providers: Array<{ id: string; name: string }>; models: Array<{ id: string; provider_id: string; model_id: string; kind: string; is_default: boolean; provider: string }> };
    const bedrock = reg.providers.find((p) => p.name === "bedrock");
    const defaultBack = async (kind: string, modelId: string) => {
      const row = reg.models.find((m) => m.provider === "bedrock" && m.model_id === modelId);
      if (!bedrock || !row) return;
      const r = await api.put(`${MP}/${bedrock.id}/models/${row.id}`, { data: { is_default: true }, timeout: 60_000 });
      done.push({ kind: `model-default-${kind}`, id: modelId, status: r.status() });
      ledger(`cleanup model default (${kind}) back to ${modelId} → ${r.status()} ${(await r.text()).slice(0, 120)}`);
    };
    for (const p of reg.providers.filter((p) => isScn(p.name))) {
      const rows = reg.models.filter((m) => m.provider_id === p.id);
      if (rows.some((m) => m.kind === "chat" && m.is_default)) await defaultBack("chat", "global.zai.glm-5.3");
      if (rows.some((m) => m.kind === "embedding" && m.is_default)) await defaultBack("embedding", "amazon.titan-embed-text-v2:0");
      for (const m of rows) await del(api, "model", `${MP}/${p.id}/models/${m.id}`, m.id, m.model_id, done);
      await del(api, "model-provider", `${MP}/${p.id}`, p.id, p.name, done);
    }
    const after = await api.get(MP, { timeout: 60_000 }).catch(() => null);
    if (after?.ok()) {
      const reg2 = (await after.json()) as { providers: Array<{ name: string }>; models: Array<{ model_id: string }> };
      registryLeft = [...reg2.providers.map((p) => p.name), ...reg2.models.map((m) => m.model_id)].filter(isScn);
    }
  } else {
    ledger(`cleanup model providers skipped (GET ${MP} → ${mpRes?.status() ?? "no response"})`);
  }

  // 0b policies (D20 s11) — tolerate an environment without the engine (503 feature.unavailable / 404)
  let policyFeature = false;
  const polRes = await api.get("/api/policies", { timeout: 60_000 }).catch(() => null);
  if (polRes?.ok()) {
    policyFeature = true;
    const pol = (await polRes.json()) as { policies: Array<{ id: string; name: string; status: string }>; settings: { mode: string } };
    if (pol.settings.mode === "enforce") {
      const r = await api.put("/api/policies/settings", { data: { mode: "log_only" } });
      done.push({ kind: "policy-mode", id: "log_only", status: r.status() });
      ledger(`cleanup policy mode enforce → log_only → ${r.status()}`);
    }
    for (const p of pol.policies.filter((p) => isScnPolicy(p.name))) {
      if (p.status === "active") {
        const r = await api.post(`/api/policies/${p.id}/retire`, { data: { reason: "scenario cleanup" } });
        done.push({ kind: "policy-retire", id: p.id, name: p.name, status: r.status() });
        ledger(`cleanup policy ${p.name} retire → ${r.status()}`);
      }
      await del(api, "policy", `/api/policies/${p.id}`, p.id, p.name, done);
    }
  } else {
    ledger(`cleanup policies skipped (GET /api/policies → ${polRes?.status() ?? "no response"})`);
  }

  // 1 canaries
  for (const c of (((await (await api.get("/api/runtime-canaries", { timeout: 60_000 })).json()) as { canaries: Array<Record<string, unknown>> }).canaries)) {
    if (!agentIds.has(String(c.champion_agent_id))) continue;
    if ((c.artifacts as Record<string, unknown>)?.cleanup) continue;
    await settleOwner(api, `/api/runtime-canaries/${c.id}`);
    if (c.status === "running") {
      const rb = await api.post(`/api/runtime-canaries/${c.id}/action`, { data: { action: "rollback" } });
      done.push({ kind: "canary-rollback", id: String(c.id), status: rb.status() });
      await settleOwner(api, `/api/runtime-canaries/${c.id}`);
    }
    const res = await api.post(`/api/runtime-canaries/${c.id}/action`, { data: { action: "cleanup" } });
    done.push({ kind: "canary-cleanup", id: String(c.id), status: res.status() });
    await settleOwner(api, `/api/runtime-canaries/${c.id}`);
  }
  // 2 (configuration experiments retired — N13-obs2: no kind to clean up any more)
  // 3 runs — stop live ones; delete the deletable rows (completed rows have no delete route — 409 is expected)
  for (const r of (((await (await api.get("/api/eval/runs?limit=200", { timeout: 60_000 })).json()) as { runs: Array<Record<string, unknown>> }).runs)) {
    if (!agentIds.has(String(r.agent_id)) && !isScn(r.name)) continue;
    if (["queued", "running", "invoking", "waiting", "evaluating"].includes(String(r.status))) {
      ledger(`cleanup run ${r.id} stop → ${(await api.post(`/api/eval/runs/${r.id}/stop`)).status()}`);
      await sleep(5000);
    }
    await del(api, "run", `/api/eval/runs/${r.id}`, String(r.id), String(r.name ?? ""), done);
  }
  // 4 online configs
  for (const c of (((await (await api.get("/api/eval/online", { timeout: 60_000 })).json()) as { configs: Array<Record<string, unknown>> }).configs)) {
    if (agentIds.has(String(c.agent_id)) || isScn(c.description)) await del(api, "online", `/api/eval/online/${c.config_id}`, String(c.config_id), String(c.description ?? ""), done);
  }
  // 5 datasets — cloud snapshots, then local rows
  for (const d of (((await (await api.get("/api/eval/datasets/cloud", { timeout: 60_000 })).json()) as { datasets: Array<Record<string, unknown>> }).datasets)) {
    const name = String(d.name ?? "");
    if (isScn(name) || name.startsWith(SCN_PREFIX.replace(/-$/, "_"))) await del(api, "cloud-dataset", `/api/eval/datasets/cloud/${d.datasetId ?? d.id}`, String(d.datasetId ?? d.id), name, done);
  }
  for (const d of (((await (await api.get("/api/eval/datasets", { timeout: 60_000 })).json()) as { datasets: Array<Record<string, unknown>> }).datasets)) {
    if (isScn(d.name)) await del(api, "dataset", `/api/eval/datasets/${d.id}`, String(d.id), String(d.name), done);
  }
  // 6 evaluators
  for (const e of (((await (await api.get("/api/eval/evaluators", { timeout: 60_000 })).json()) as { evaluators: Array<Record<string, unknown>> }).evaluators)) {
    if (isScnEvaluator(e.name)) await del(api, "evaluator", `/api/eval/evaluators/${e.id}`, String(e.id), String(e.name), done);
  }
  // 7 agents — consumer first (HR assistant mounts the payroll peer), provider last; log groups must go LAST (N10v4-obs9)
  const order = (a: Record<string, unknown>) => (/-hr-runtime-/.test(String(a.name)) ? 1 : /-payroll-/.test(String(a.name)) ? 2 : 0);
  const logGroups: Array<{ name: string; group: string; removed: string[] }> = [];
  // N12-obs61: export every runtime log group (DEFAULT + canary stable/treat arms) to the evidence bucket BEFORE the
  // product's teardown deletes them (SCENARIO_LOG_EXPORT=0 skips); the exported list goes to the ledger + attachment
  const runtimeIdsForExport: string[] = [];
  for (const a of agents) {
    const full = (await (await api.get(`/api/agents/${a.id}`, { timeout: 60_000 })).json().catch(() => ({}))) as Record<string, unknown>;
    const rid = runtimeIdOf(full);
    if (rid) runtimeIdsForExport.push(rid);
  }
  const exported = exportRuntimeLogs(runtimeIdsForExport, String(stateGet("stamp") ?? new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "")), "cleanup");
  await record(testInfo, "runtime-logs-export.json", exported, `${exported.filter((g) => g.status === "uploaded").length} groups exported of ${exported.length}`);
  for (const a of [...agents].sort((x, y) => order(x) - order(y))) {
    expect(a.system_key ?? null, "never a system preset").toBeNull();
    const full = (await (await api.get(`/api/agents/${a.id}`, { timeout: 60_000 })).json().catch(() => ({}))) as { arn?: string; resource_id?: string };
    const runtimeId = String(full.resource_id ?? String(full.arn ?? "").split("/").pop() ?? "");
    const res = await api.delete(`/api/agents/${a.id}`, { timeout: 180_000 });
    const body = (await res.json().catch(() => ({}))) as { removed?: string[]; failed?: string[] };
    done.push({ kind: "agent", id: String(a.id), name: String(a.name), status: res.status() });
    ledger(`cleanup agent ${a.name} DELETE → ${res.status()} removed=${JSON.stringify(body.removed)} failed=${JSON.stringify(body.failed)}`);
    if (runtimeId) logGroups.push({ name: String(a.name), group: `/aws/bedrock-agentcore/runtimes/${runtimeId}-DEFAULT`, removed: body.removed ?? [] });
    for (let i = 0; i < 30 && (await listAgents(api)).some((x) => x.id === a.id); i++) await sleep(10_000);
  }
  // 8 knowledge bases
  for (const kb of (((await (await api.get("/api/knowledge-bases", { timeout: 60_000 })).json()) as { items: Array<{ kb_id: string; name: string }> }).items)) {
    if (isScn(kb.name)) await del(api, "kb", `/api/knowledge-bases/${kb.kb_id}`, kb.kb_id, kb.name, done);
  }
  // 9 orphan registry records (A2A rows the teardown may have left) — never the seeded skills / gateway records
  for (const r of (await registryRecords(api)).filter((r) => isScn(r.name))) await del(api, "registry-record", `/api/registry/records/${r.record_id}`, r.record_id, r.name, done);
  // 10 Skill Lab: this run's jobs (bounded by run_start) + e2es task sets
  // No state file (an interrupted run cleaned from a fresh checkout): bound the time-scoped deletions (Skill Lab jobs,
  // API keys) by SCENARIO_CLEANUP_SINCE or by the `-<MMDDHHmm>` stamp of the discovered e2es agents — never by epoch 0,
  // which would delete every job / key in the workspace.
  const runStart = state.run_start ?? process.env.SCENARIO_CLEANUP_SINCE ?? stampStart(agents.map((a) => String(a.name)));
  ledger(`cleanup time-scoped deletions since ${runStart ?? "(unknown — skipped)"}`);
  const jobs = ((await (await api.get("/api/skill-lab/jobs", { timeout: 60_000 })).json().catch(() => [])) ?? []) as Array<{ id: string; type: string; status: string; created_at?: string }>;
  for (const j of jobs.filter((j) => runStart && j.created_at && String(j.created_at) >= runStart)) {
    if (["queued", "running"].includes(j.status)) await api.post(`/api/skill-lab/jobs/${j.id}/cancel`).catch(() => null);
    await del(api, "skill-lab-job", `/api/skill-lab/jobs/${j.id}`, j.id, `${j.type} ${j.status}`, done);
  }
  for (const t of (((await (await api.get("/api/skill-lab/tasksets", { timeout: 60_000 })).json().catch(() => [])) ?? []) as Array<{ id: string; name: string }>).filter((t) => isScn(t.name))) {
    await del(api, "skill-lab-taskset", `/api/skill-lab/tasksets/${t.id}`, t.id, t.name, done);
  }
  // 10b API keys minted by this run (workspace-wide rows that outlive their agents — N-SCN-obs4)
  const keysBody = (await (await api.get("/api/apikeys", { timeout: 60_000 })).json().catch(() => ({}))) as { keys?: Array<Record<string, unknown>>; items?: Array<Record<string, unknown>> };
  for (const k of (keysBody.keys ?? keysBody.items ?? []).filter((k) => runStart && k.created_at && String(k.created_at) >= runStart)) {
    await del(api, "api-key", `/api/apikeys/${k.id}`, String(k.id), String(k.prefix ?? k.name ?? ""), done);
  }
  // 11 the member account
  for (const u of (((await (await api.get("/api/users", { timeout: 60_000 })).json()) as { items: Array<{ id: string; username: string }> }).items).filter((u) => u.username.startsWith(`${SCN_PREFIX}member-`))) {
    await del(api, "user", `/api/users/${u.id}`, u.id, u.username, done);
  }

  // ── verification (product + read-only AWS) ──
  await sleep(15_000);
  const left = {
    agents: (await listAgents(api)).filter((a) => isScn(a.name)).map((a) => `${a.name}:${a.status}`),
    kbs: (((await (await api.get("/api/knowledge-bases", { timeout: 60_000 })).json()) as { items: Array<{ name: string }> }).items).filter((k) => isScn(k.name)).map((k) => k.name),
    datasets: (((await (await api.get("/api/eval/datasets", { timeout: 60_000 })).json()) as { datasets: Array<{ name: string }> }).datasets).filter((d) => isScn(d.name)).map((d) => d.name),
    evaluators: (((await (await api.get("/api/eval/evaluators", { timeout: 60_000 })).json()) as { evaluators: Array<{ name?: string }> }).evaluators).filter((e) => isScnEvaluator(e.name)).map((e) => e.name),
    online: (((await (await api.get("/api/eval/online", { timeout: 60_000 })).json()) as { configs: Array<{ description?: string; agent_id?: string }> }).configs).filter((c) => isScn(c.description) || agentIds.has(String(c.agent_id))).map((c) => c.description),
    records: (await registryRecords(api)).filter((r) => isScn(r.name)).map((r) => r.name),
    policies: policyFeature
      ? (((await (await api.get("/api/policies", { timeout: 60_000 })).json()) as { policies: Array<{ name: string; status: string }> }).policies).filter((p) => isScnPolicy(p.name)).map((p) => `${p.name}:${p.status}`)
      : [],
    model_providers: registryLeft,
  };
  const policyMode = policyFeature ? String((((await (await api.get("/api/policies/settings", { timeout: 60_000 })).json()) as { mode?: string }).mode) ?? "") : "n/a";
  const runtimes = awsRead("bedrock-agentcore-control list-agent-runtimes");
  const scnRuntimes = (runtimes.match(/"agentRuntimeName":\s*"e2es[^"]*"/g) ?? []).map((s) => s.split('"')[3]);
  const roles = awsRead(`iam list-roles --query "Roles[?starts_with(RoleName, 'launchpad-agent-${SCN_PREFIX}')].RoleName"`);
  // r7-G/H: the teardown settles a re-created runtime log group with a background re-check (≤ 75 s after the delete),
  // and describe-log-groups is eventually consistent — poll up to 2 min before calling a group residue (N-SCN-obs8)
  const listLeft = () => logGroups.filter((lg) => awsRead(`logs describe-log-groups --log-group-name-prefix ${lg.group} --query "logGroups[].logGroupName"`).includes(lg.group)).map((lg) => lg.group);
  let logLeft = listLeft();
  for (let i = 0; i < 8 && logLeft.length; i++) {
    ledger(`cleanup log groups still listed (+${15 * (i + 1)}s): ${logLeft.join(",")}`);
    await sleep(15_000);
    logLeft = listLeft();
  }
  for (const lg of logGroups) if (lg.removed.includes("log_groups")) expect(lg.removed[lg.removed.length - 1], `${lg.name}: log_groups is the LAST teardown step`).toBe("log_groups");
  // N-SCN-obs20 (test-side): a 2xx (or 404 — already gone) is a deletion; anything else (409 `run.not_deletable` on the
  // completed runs, N12-obs38) is a refused attempt and is reported separately instead of being counted as deleted.
  const isDeleted = (d: Done): boolean => { const st = Number(d.status); return (st >= 200 && st < 300) || st === 404; };
  const deleted = done.filter(isDeleted);
  const refused = done.filter((d) => !isDeleted(d));
  await record(testInfo, "cleanup-summary.json", { deleted, refused, left, runtimes_left: scnRuntimes, roles_left: roles.slice(0, 300), log_groups_left: logLeft }, `deleted ${deleted.length}; refused ${refused.length}${refused.length ? ` (${refused.map((d) => `${d.kind} ${d.id} ${d.status}`).join(", ")})` : ""}; left ${JSON.stringify(left)}; policy mode=${policyMode}; runtimes=${scnRuntimes.length} logGroups=${logLeft.length}`);
  expect(left.agents, "no e2es agents left").toEqual([]);
  expect(left.kbs, "no e2es knowledge bases left").toEqual([]);
  expect(left.datasets, "no e2es datasets left").toEqual([]);
  expect(left.evaluators, "no e2es evaluators left").toEqual([]);
  expect(left.online, "no e2es online configs left").toEqual([]);
  expect(left.records, "no e2es registry records left").toEqual([]);
  expect(left.policies, "no e2es_ policies left").toEqual([]);
  expect(left.model_providers, "no e2es- model provider / row left (D21 s12)").toEqual([]);
  if (policyFeature && policyMode !== "log_only") ledger(`soft: policy mode after cleanup is ${policyMode} (expected log_only — the cleanup only resets enforce)`);
  expect(scnRuntimes, "no e2es runtime on AWS (read-only list-agent-runtimes)").toEqual([]);
  expect(/launchpad-agent-e2es/.test(roles), "no e2es execution role on AWS").toBe(false);
  expect(logLeft, "no e2es log group re-created after the deletes").toEqual([]);
  ledger(`cleanup done: ${deleted.length} deletions, ${refused.length} refused`);
});

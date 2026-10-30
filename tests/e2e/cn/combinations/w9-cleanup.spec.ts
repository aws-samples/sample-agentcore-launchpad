/**
 * Cleanup of every round-16 combination fixture: x16n-* agents (their AWS resources go with
 * the ledger row), x16n-* runs / online configs, datasets, registry records, the x16n-member
 * account, and the default workspace's visibility mode back to `shared` (its value before
 * the suites ran — X16N_RESTORE_VIS overrides). Verifies the sweep and writes cleanup.json.
 */
import { expect, test } from "@playwright/test";

import * as L from "./lib";

const RESTORE_VIS = (process.env.X16N_RESTORE_VIS ?? "shared") as "shared" | "isolated";

test("cleanup: x16n fixtures gone, member deleted, visibility restored", async ({ browser }) => {
  const admin = await L.session(browser, "admin");
  const report: Record<string, unknown> = {};
  report.agents = await L.deleteAgentsWithPrefix(admin.api);
  report.tasks = await L.deleteTasksWithPrefix(admin.api);
  report.datasets = await L.deleteDatasetsWithPrefix(admin.api);
  report.memories = await L.deleteMemoryResourcesWithPrefix(admin.api);
  report.evaluators = await L.deleteEvaluatorsWithPrefix(admin.api);
  const recs = ((await admin.api.get("/api/registry/records")).body?.records ?? []) as { record_id: string; name: string }[];
  const regGone: string[] = [];
  for (const r of recs) {
    if (!r.name.startsWith(L.PREFIX)) continue;
    const d = await admin.api.delete(`/api/registry/records/${encodeURIComponent(r.record_id)}`);
    if (d.status < 300) regGone.push(r.name);
  }
  report.registry = regGone;
  report.member = await L.deleteMember(admin.api);
  if ((await L.getVisibility(admin.api)) !== RESTORE_VIS) await L.setVisibility(admin.api, RESTORE_VIS);
  // verify
  await L.sleep(2000);
  const left = {
    agents: (await L.listAgents(admin.api)).filter((a) => a.name.startsWith(L.PREFIX)).map((a) => `${a.name}:${a.status}`),
    runs: (((await admin.api.get("/api/eval/runs?limit=200")).body?.runs ?? []) as { name?: string; status: string; id: string }[]).filter((r) => (r.name ?? "").startsWith(L.PREFIX)).map((r) => `${r.id}:${r.status}`),
    online: (((await admin.api.get("/api/eval/online")).body?.configs ?? []) as { description?: string; config_id: string }[]).filter((c) => (c.description ?? "").startsWith(L.PREFIX)).map((c) => c.config_id),
    datasets: (((await admin.api.get("/api/eval/datasets")).body?.datasets ?? []) as { name: string }[]).filter((d) => d.name.startsWith(L.PREFIX)).map((d) => d.name),
    registry: (((await admin.api.get("/api/registry/records")).body?.records ?? []) as { name: string }[]).filter((r) => r.name.startsWith(L.PREFIX)).map((r) => r.name),
    member: (((await admin.api.get(`/api/users?q=${L.MEMBER}&limit=50`)).body?.items ?? []) as { username: string }[]).filter((u) => u.username === L.MEMBER).map((u) => u.username),
    memories: (((await admin.api.get("/api/memory/resources")).body?.items ?? []) as { name: string | null; is_default: boolean }[]).filter((m) => !m.is_default && (m.name ?? "").startsWith("x16n")).map((m) => m.name),
    evaluators: (((await admin.api.get("/api/eval/evaluators")).body?.evaluators ?? []) as { id: string; name?: string | null; source: string }[]).filter((e) => e.source === "custom" && (e.name ?? e.id).startsWith("x16n")).map((e) => e.name ?? e.id),
    visibility: await L.getVisibility(admin.api),
  };
  report.left = left;
  L.writeJson("cleanup.json", report);
  L.log("cleanup", report);
  await admin.ctx.close();
  expect.soft(left.agents, "x16n agents left (a row still deploying is kept on purpose — N16-obs83)").toEqual([]);
  expect(left.memories, "x16n memory resources left").toEqual([]);
  expect(left.evaluators, "x16n evaluators left").toEqual([]);
  expect(left.datasets, "x16n datasets left").toEqual([]);
  expect(left.registry, "x16n registry records left").toEqual([]);
  expect(left.member, "member left").toEqual([]);
  expect(left.visibility).toBe(RESTORE_VIS);
  expect.soft(left.runs, "x16n runs left (a running run cannot be deleted until stopped)").toEqual([]);
  expect.soft(left.online, "x16n online configs left").toEqual([]);
});

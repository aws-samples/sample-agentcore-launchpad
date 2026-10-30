import { expect, test } from "@playwright/test";
import { activeAgent, attach, evidence, ledger, sleep } from "./helpers";

/**
 * Case 20 (v3) — Memory admin page: browse actors / sessions / events / records for the
 * case-2/6 agent; the counts match the Chat memory rail; delete one long-term record through
 * the UI (row action → confirm) → count −1; delete a second one through
 * `DELETE /api/memory/records/{id}?actor_id=` → count −1 again (N11 r3-C, N10v2-bug6).
 * The "delete the agent → its records purged" half lives in case 17.
 */
test("case 20: memory page matches the Chat rail; UI delete → −1; API delete (actor_id) → −1", async ({ page }, testInfo) => {
  const a = await activeAgent(page.request, "harness");
  expect(a, "case 2 agent required").toBeTruthy();
  const sessions = (await (await page.request.get(`/api/chat/${a!.id}/sessions`)).json()).sessions as Array<{ session_id: string }>;
  expect(sessions.length).toBeGreaterThan(0);
  const sid = sessions[0].session_id;
  const rail = await (await page.request.get(`/api/chat/${a!.id}/memory?session_id=${encodeURIComponent(sid)}`)).json();
  const actorId: string = rail.actor_id;
  ledger(`case20 start agent=${a!.name} actor=${actorId} session=${sid} rail events=${rail.event_count} records=${(rail.records ?? []).length}`);

  // API consistency: events of the session, records of the actor
  const actors = await (await page.request.get("/api/memory/actors")).json();
  const sess = await (await page.request.get(`/api/memory/sessions?actor_id=${encodeURIComponent(actorId)}`)).json();
  const events = await (await page.request.get(`/api/memory/events?actor_id=${encodeURIComponent(actorId)}&session_id=${encodeURIComponent(sid)}`)).json();
  const namespaces = ((await (await page.request.get(`/api/memory/namespaces?actor_id=${encodeURIComponent(actorId)}`)).json()).items ?? []) as Array<{ strategy_id: string; namespace: string }>;
  type Rec = { record_id?: string; id?: string; namespace?: string };
  const listAll = async (): Promise<{ all: Rec[]; perStrategy: Record<string, number> }> => {
    const perStrategy: Record<string, number> = {};
    let all: Rec[] = [];
    for (const ns of namespaces) {
      const r = await (await page.request.get(`/api/memory/records?actor_id=${encodeURIComponent(actorId)}&strategy_id=${encodeURIComponent(ns.strategy_id)}&max_results=100`)).json();
      const items = (r.items ?? r.records ?? []) as Rec[];
      perStrategy[ns.strategy_id] = items.length;
      all = all.concat(items.map((x) => ({ ...x, namespace: x.namespace ?? ns.namespace })));
    }
    return { all, perStrategy };
  };
  const { all: allRecords, perStrategy } = await listAll();
  await attach(testInfo, "memory-api.json", { actors, sess, events, records: allRecords, perStrategy, rail });
  const actorList = (actors.items ?? actors.actors ?? []) as Array<{ actor_id?: string; id?: string }>;
  const eventList = (events.items ?? events.events ?? []) as unknown[];
  ledger(`case20 api actors=${actorList.length} sessions=${(sess.items ?? sess.sessions ?? []).length} events=${eventList.length} records=${allRecords.length} per-strategy=${JSON.stringify(perStrategy)}`);
  expect(actorList.some((x) => (x.actor_id ?? x.id) === actorId)).toBe(true);
  expect(eventList.length).toBe(rail.event_count);
  const railNamespaces = new Set(((rail.records ?? []) as Array<{ namespace?: string }>).map((r) => r.namespace));
  const pageNamespaces = new Set(allRecords.map((r) => r.namespace));
  const prefixes = namespaces.map((n) => n.namespace.replace(/\/$/, ""));
  for (const ns of railNamespaces) {
    const covered = pageNamespaces.has(ns) || prefixes.some((p) => String(ns).startsWith(p));
    expect(covered, `rail namespace ${ns} on the Memory page`).toBe(true);
  }
  expect(allRecords.length).toBeGreaterThanOrEqual((rail.records ?? []).length);
  expect(allRecords.length, "needs ≥ 2 long-term records (case 6)").toBeGreaterThanOrEqual(2);

  // UI browse
  await page.goto(`/v2/memory?tab=short-term&actor=${encodeURIComponent(actorId)}&session=${encodeURIComponent(sid)}`);
  await expect(page.getByTestId("v2-memory-actors")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("v2-memory-sessions")).toBeVisible();
  await expect(page.getByTestId("v2-memory-event").first()).toBeVisible({ timeout: 60_000 });
  const uiEvents = await page.getByTestId("v2-memory-event").count();
  await evidence(page, "case20-memory-short-term");
  expect(uiEvents).toBe(rail.event_count);
  // the long-term tab lists one strategy at a time: pick the strategy with the most records
  const strategy = Object.entries(perStrategy).sort((x, y) => y[1] - x[1])[0][0];
  await page.goto(`/v2/memory?tab=long-term&actor=${encodeURIComponent(actorId)}&strategy=${encodeURIComponent(strategy)}`);
  await expect(page.getByTestId("v2-memory-longterm")).toBeVisible({ timeout: 60_000 });
  const rows = page.locator('[data-testid="v2-memory-records"] tbody tr').filter({ hasText: /./ });
  await expect(rows).toHaveCount(perStrategy[strategy], { timeout: 60_000 });
  const uiRecords = await rows.count();
  await evidence(page, "case20-memory-long-term");
  ledger(`case20 ui events=${uiEvents} records(${strategy})=${uiRecords} api=${perStrategy[strategy]}`);
  expect(uiRecords, `${strategy} records on the page == API`).toBe(perStrategy[strategy]);

  // ── delete ONE record through the UI row action → −1 ─────────────────────
  const target = allRecords.filter((r) => r.namespace && prefixes.some((p) => String(r.namespace).startsWith(p) && namespaces.find((n) => n.namespace.replace(/\/$/, "") === p)?.strategy_id === strategy))[0] ?? allRecords[0];
  const targetId = String(target.record_id ?? target.id);
  const rowDelete = page.getByTestId(`v2-memory-record-delete-${targetId}`);
  await expect(rowDelete, "row Delete action exists").toBeVisible({ timeout: 30_000 });
  await rowDelete.click();
  const ok = page.getByTestId("v2-confirm-ok");
  await expect(ok).toBeVisible({ timeout: 10_000 });
  await evidence(page, "case20-record-delete-confirm");
  const delRes = page.waitForResponse((r) => r.url().includes("/api/memory/records/") && r.request().method() === "DELETE");
  await ok.click();
  const dr = await delRes;
  const drBody = await dr.text();
  ledger(`case20 UI delete ${targetId} → ${dr.status()} ${drBody.slice(0, 160)} url=${new URL(dr.url()).search}`);
  expect(dr.status(), "UI delete → 200").toBe(200);
  expect(new URL(dr.url()).searchParams.get("actor_id"), "UI delete passes actor_id").toBe(actorId);
  // the extractor may still be adding records for other namespaces (case 6 just ran), so the
  // −1 is asserted on the deleted record's own strategy namespace and on the id, not on the sum
  const has = (list: Rec[], id: string) => list.some((r) => String(r.record_id ?? r.id) === id);
  let after1 = await listAll();
  for (let i = 0; i < 5 && has(after1.all, targetId); i++) {
    await sleep(3000);
    after1 = await listAll();
  }
  await expect(rows).toHaveCount(perStrategy[strategy] - 1, { timeout: 60_000 });
  await evidence(page, "case20-memory-long-term-after-ui-delete");
  ledger(`case20 after UI delete: ${strategy} ${perStrategy[strategy]} → ${after1.perStrategy[strategy]}, total ${allRecords.length} → ${after1.all.length} (other namespaces may still be extracting), page rows → ${await rows.count()}`);
  expect(has(after1.all, targetId), "deleted record no longer listed").toBe(false);
  expect(after1.perStrategy[strategy], `UI delete → ${strategy} count −1`).toBe(perStrategy[strategy] - 1);

  // ── delete a SECOND record through the API with actor_id → −1 ───────────
  const second = after1.all.find((r) => String(r.record_id ?? r.id) !== targetId)!;
  const secondId = String(second.record_id ?? second.id);
  const noActor = await page.request.delete(`/api/memory/records/${encodeURIComponent(secondId)}`);
  ledger(`case20 API delete without actor_id → ${noActor.status()} (422 expected)`);
  const apiDel = await page.request.delete(`/api/memory/records/${encodeURIComponent(secondId)}?actor_id=${encodeURIComponent(actorId)}`);
  const apiBody = await apiDel.text();
  ledger(`case20 API delete ${secondId} → ${apiDel.status()} ${apiBody.slice(0, 160)}`);
  await attach(testInfo, "api-delete.json", { without_actor: noActor.status(), status: apiDel.status(), body: apiBody });
  const secondStrategy = namespaces.find((n) => String(second.namespace ?? "").startsWith(n.namespace.replace(/\/$/, "")))?.strategy_id ?? strategy;
  let after2 = await listAll();
  for (let i = 0; i < 5 && has(after2.all, secondId); i++) {
    await sleep(3000);
    after2 = await listAll();
  }
  ledger(`case20 after API delete: ${secondStrategy} ${after1.perStrategy[secondStrategy]} → ${after2.perStrategy[secondStrategy]}, total ${after1.all.length} → ${after2.all.length}`);
  expect(noActor.status(), "DELETE without actor_id is refused").toBe(422);
  expect(apiDel.status(), "API delete → 200").toBe(200);
  expect(has(after2.all, secondId), "API-deleted record no longer listed").toBe(false);
  expect(has(after2.all, targetId)).toBe(false);
  expect(after2.perStrategy[secondStrategy], `API delete → ${secondStrategy} count −1`).toBe(after1.perStrategy[secondStrategy] - 1);
  ledger("case20 end PASS");
});

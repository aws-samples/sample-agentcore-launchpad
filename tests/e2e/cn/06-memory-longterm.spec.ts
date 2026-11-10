import { expect, test } from "@playwright/test";
import { activeAgent, attach, chatOnce, evidence, ledger, sleep } from "./helpers";

/**
 * Case 6 — long-term memory on the case-2 agent (memory.long_term=true): session 1 tells
 * two facts → extraction (SQS → Lambda → OpenSearch) yields ≥ 2 long-term records
 * within 10 min → a NEW session answers both facts (2/2).
 */
test("case 6: two facts → ≥2 long-term records ≤10 min → second session recalls 2/2", async ({ page }, testInfo) => {
  const a = await activeAgent(page.request, "harness");
  expect(a, "case 2 must have deployed the e2e2-harness agent (long-term memory on)").toBeTruthy();
  const agentId = a!.id;
  ledger(`case6 start agent=${a!.name}`);

  const s1 = await chatOnce(page, agentId, "Please remember two things about me: my favorite color is teal, and my project codename is Nebula.");
  ledger(`case6 facts acknowledged ${s1.seconds}s: ${s1.answer.slice(0, 160).replace(/\n/g, " ")}`);
  expect(s1.errors).toBe(0);
  const mem = await (await page.request.get(`/api/chat/${agentId}/memory?session_id=${encodeURIComponent(s1.sessionId)}`)).json();
  await attach(testInfo, "memory-summary-1.json", mem);
  const actorId: string = mem.actor_id;
  await evidence(page, "case06-session1-facts");

  // Memory page, short-term tab: events of this session visible
  await page.goto(`/v2/memory?tab=short-term&actor=${encodeURIComponent(actorId)}&session=${encodeURIComponent(s1.sessionId)}`);
  await expect(page.getByTestId("v2-memory-event").first()).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case06-memory-short-term");

  // ≤ 10 min for ≥ 2 long-term records
  const tWait = Date.now();
  let records: unknown[] = [];
  let jobs: unknown = null;
  while (Date.now() - tWait < 10 * 60 * 1000) {
    const s = await (await page.request.get(`/api/chat/${agentId}/memory?session_id=${encodeURIComponent(s1.sessionId)}`)).json();
    records = s.records ?? [];
    if (records.length >= 2) break;
    if (Math.round((Date.now() - tWait) / 1000) % 60 < 10) {
      jobs = await (await page.request.get(`/api/memory/extraction-jobs?actor_id=${encodeURIComponent(actorId)}`)).json();
      ledger(`case6 waiting for long-term records… ${Math.round((Date.now() - tWait) / 1000)}s records=${records.length} jobs=${JSON.stringify(jobs).slice(0, 200)}`);
    }
    await sleep(10_000);
  }
  const extractSeconds = Math.round((Date.now() - tWait) / 1000);
  await attach(testInfo, "extraction-jobs.json", jobs ?? {});
  await attach(testInfo, "records.json", records);
  ledger(`case6 long-term records=${records.length} after ${extractSeconds}s`);
  await page.goto(`/v2/memory?tab=long-term&actor=${encodeURIComponent(actorId)}`);
  await expect(page.getByTestId("v2-memory-longterm")).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case06-memory-long-term");

  // second session
  const s2 = await chatOnce(page, agentId, "What is my favorite color, and what is my project codename? Answer with both.");
  await attach(testInfo, "recall.txt", s2.answer);
  await evidence(page, "case06-recall-second-session");
  ledger(`case6 recall ${s2.seconds}s session=${s2.sessionId}: ${s2.answer.slice(0, 200).replace(/\n/g, " ")}`);
  expect(s2.sessionId).not.toBe(s1.sessionId);
  const hits = [/teal/i.test(s2.answer), /nebula/i.test(s2.answer)].filter(Boolean).length;
  ledger(`case6 facts recalled ${hits}/2 extract=${extractSeconds}s records=${records.length}`);
  expect(records.length, "≥ 2 long-term records within 10 min").toBeGreaterThanOrEqual(2);
  expect(extractSeconds).toBeLessThanOrEqual(600);
  expect(hits, "session 2 must recall 2/2 facts").toBe(2);
  ledger(`case6 end PASS agent=${agentId} s1=${s1.sessionId} s2=${s2.sessionId}`);
});

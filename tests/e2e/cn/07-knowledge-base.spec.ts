import { expect, test } from "@playwright/test";
import path from "node:path";
import { attach, E2E_PREFIX, evidence, ledger, REPO_ROOT, sleep } from "./helpers";

/** Case 7 (part 1) — create KB → upload samples/kb_docs → ingestion completes → retrieval playground hits. */
test("case 7a: create KB, upload kb_docs, ingestion completes, retrieval hits", async ({ page }, testInfo) => {
  const name = `${E2E_PREFIX}aurora-kb-${new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "")}`;
  ledger(`case7 start name=${name}`);
  await page.goto("/v2/knowledge-bases");
  await page.getByTestId("v2-kb-new").click();
  await page.getByTestId("v2-kb-name").fill(name);
  await page.getByTestId("v2-kb-desc").fill("N10 e2e — Aurora Deck docs");
  await page.getByTestId("v2-kb-mode-upload").click();
  const docs = path.join(REPO_ROOT, "samples/kb_docs");
  await page.getByTestId("v2-kb-file-input").setInputFiles([
    path.join(docs, "aurora-deck-overview.md"),
    path.join(docs, "aurora-deck-support.md"),
  ]);
  await expect(page.getByTestId("v2-kb-files").locator("li")).toHaveCount(2);
  await evidence(page, "case07-kb-create");
  const t0 = Date.now();
  await page.getByTestId("v2-kb-submit").click();
  await expect(page).toHaveURL(/view=detail&id=/, { timeout: 120_000 });
  const kbId = new URL(page.url()).searchParams.get("id")!;
  ledger(`case7 kb created id=${kbId}`);

  // stay on the detail page (it triggers the first sync once the source is AVAILABLE) and poll the API
  let detail: Record<string, unknown> = {};
  let done = false;
  for (let i = 0; i < 90 && !done; i++) {
    const res = await page.request.get(`/api/knowledge-bases/${kbId}`);
    if (res.ok()) {
      detail = await res.json();
      const sources = (detail.data_sources as Array<{ status: string; ingestion_jobs?: Array<{ status: string }> }>) ?? [];
      const jobs = sources.flatMap((s) => s.ingestion_jobs ?? []);
      const st = `${detail.status} ds=${sources.map((s) => s.status).join(",")} jobs=${jobs.map((j) => j.status).join(",")}`;
      if (i % 4 === 0) ledger(`case7 kb ${st}`);
      done = jobs.some((j) => /COMPLETE|SUCCEEDED/i.test(j.status));
      if (jobs.some((j) => /FAILED/i.test(j.status))) break;
    }
    if (!done) await sleep(10_000);
  }
  await attach(testInfo, "kb-detail.json", detail);
  const ingestSeconds = Math.round((Date.now() - t0) / 1000);
  await page.reload();
  await expect(page.getByTestId("v2-kb-sources-table")).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case07-kb-ingested");
  expect(done, `ingestion did not complete in ${ingestSeconds}s`).toBe(true);
  expect(ingestSeconds, "ingestion ≤ 15 min").toBeLessThanOrEqual(15 * 60);
  ledger(`case7 ingestion complete in ${ingestSeconds}s`);
  // indexed documents == uploaded files (2)
  const ds = ((detail.data_sources as Array<{ ds_id?: string; id?: string }>) ?? [])[0];
  const dsId = ds?.ds_id ?? ds?.id;
  let indexed = -1;
  for (let i = 0; i < 12 && indexed !== 2; i++) {
    const docs = await (await page.request.get(`/api/knowledge-bases/${kbId}/data-sources/${dsId}/documents`)).json();
    indexed = ((docs.documents ?? []) as unknown[]).length;
    if (indexed !== 2) await sleep(5000);
  }
  ledger(`case7 indexed documents=${indexed} (uploaded 2)`);
  expect(indexed, "indexed document count == uploaded count").toBe(2);

  // retrieval playground (behind the "Retrieve test" tab)
  const tab = page.getByRole("tab", { name: /retrieve/i }).or(page.getByText(/^Retrieve test$/));
  if (await tab.first().isVisible().catch(() => false)) await tab.first().click();
  await expect(page.getByTestId("v2-kb-query")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("v2-kb-query").fill("What is the maximum number of slides per deck?");
  await page.getByTestId("v2-kb-search-btn").click();
  await expect(page.getByTestId("v2-kb-hit").first()).toBeVisible({ timeout: 120_000 });
  const hits = await page.getByTestId("v2-kb-hits").innerText();
  await attach(testInfo, "kb-hits.txt", hits);
  await evidence(page, "case07-kb-retrieve");
  expect(hits).toMatch(/400/);
  // top-1 hit must come from the expected document (overview carries the slide limit)
  const top1 = await page.getByTestId("v2-kb-hit").first().innerText();
  ledger(`case7 top-1 hit: ${top1.slice(0, 160).replace(/\n/g, " ")}`);
  expect(top1).toMatch(/aurora-deck-overview/);
  ledger(`case7a end PASS kb=${kbId} name=${name} hits=${await page.getByTestId("v2-kb-hit").count()} ingest=${ingestSeconds}s indexed=${indexed}`);
});

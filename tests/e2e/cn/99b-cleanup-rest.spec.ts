import { expect, test } from "@playwright/test";
import { E2E_PREFIX, evidence, isE2E, ledger, listAgents } from "./helpers";

/**
 * Second cleanup pass: KBs are deleted from their detail page, registry records from the
 * list. Deletes the e2e KB(s), the imported sample skill, and the A2A records orphaned by
 * agents deleted in 99-cleanup (N10-bug7). Keeps the kept agent's own A2A record and the
 * platform MCP records (hr-database / office-facts).
 */
test("cleanup (rest): e2e KBs via detail page, orphan A2A + skill records via registry list", async ({ page }) => {
  // N10_KEEP: a prefix ("harness" → keep every e2e2-harness-*) or an exact agent name ("e2e2-harness-10100823")
  const keep = process.env.N10_KEEP ?? "none";
  const isKept = (name: unknown) => String(name) === keep || String(name).startsWith(`${E2E_PREFIX}${keep}-`);
  const liveNames = new Set((await listAgents(page.request)).map((a) => String(a.name)));

  const kbs = ((await (await page.request.get("/api/knowledge-bases")).json()).items as Array<{ kb_id: string; name: string }>).filter((k) => isE2E(k.name));
  for (const kb of kbs) {
    await page.goto(`/v2/knowledge-bases?view=detail&id=${encodeURIComponent(kb.kb_id)}`);
    const btn = page.getByTestId("v2-kb-delete");
    await expect(btn).toBeVisible({ timeout: 30_000 });
    await btn.click();
    await page.getByTestId("v2-confirm-ok").click();
    ledger(`cleanup kb ${kb.name} (${kb.kb_id}) delete requested (detail page)`);
    await page.waitForTimeout(3000);
  }

  const records = (await (await page.request.get("/api/registry/records")).json()).records as Array<{ record_id: string; name: string; type?: string; descriptor_type?: string }>;
  const victims = records.filter((r) => {
    const type = r.type ?? r.descriptor_type;
    if (type === "A2A" && isE2E(r.name) && !isKept(r.name) && !liveNames.has(r.name)) return true;
    return false;
  });
  await page.goto("/v2/registry");
  await expect(page.getByTestId("v2-registry-table")).toBeVisible();
  for (const r of victims) {
    const btn = page.getByTestId(`v2-registry-delete-${r.record_id}`);
    await expect(btn).toBeVisible({ timeout: 15_000 });
    await btn.click();
    await page.getByTestId("v2-confirm-ok").click();
    ledger(`cleanup registry record ${r.name} (${r.record_id}) delete requested`);
    await page.waitForTimeout(1500);
  }
  await page.reload();
  await evidence(page, "cleanup-registry-after");

  const remainingAgents = (await listAgents(page.request)).map((a) => `${a.name}:${a.status}`);
  const remainingKbs = ((await (await page.request.get("/api/knowledge-bases")).json()).items as Array<{ name: string; status: string }>).map((k) => `${k.name}:${k.status}`);
  const remainingRecords = ((await (await page.request.get("/api/registry/records")).json()).records as Array<{ name: string; status: string }>).map((r) => `${r.name}:${r.status}`);
  ledger(`cleanup(rest) end agents=${JSON.stringify(remainingAgents)} kbs=${JSON.stringify(remainingKbs)} records=${JSON.stringify(remainingRecords)}`);
});

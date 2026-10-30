import { expect, test } from "@playwright/test";
import { execSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { approveRecordViaUi, attach, evidence, isE2E, ledger, listAgents, REPO_ROOT } from "./helpers";

/** Case 8 (part 1) — a registry record exists for a deployed agent; import skill → APPROVED. */
test("case 8a: deployed agents have registry records; import expense-report-writer and approve it", async ({ page }, testInfo) => {
  ledger("case8 start");
  const agents = await listAgents(page.request);
  const deployed = agents.filter((a) => isE2E(a.name) && a.status === "active");
  const records = (await (await page.request.get("/api/registry/records")).json()).records as Array<Record<string, unknown>>;
  await attach(testInfo, "registry-records.json", records);
  const agentRecords = records.filter((r) => deployed.some((a) => a.name === r.name));
  ledger(`case8 active e2e agents=${deployed.map((a) => a.name).join(",")} agent-records=${agentRecords.map((r) => `${r.name}:${r.record_type ?? r.type}:${r.status}`).join(",")}`);
  await page.goto("/v2/registry");
  await expect(page.getByTestId("v2-registry-table")).toBeVisible();
  await evidence(page, "case08-registry-list");
  expect(deployed.length, "needs at least one deployed e2e agent (cases 2–5)").toBeGreaterThan(0);
  expect(agentRecords.length, "a registry (A2A/AGENT) record should exist for a deployed agent").toBeGreaterThan(0);
  // one auto-created A2A record per deployed agent, each in a valid lifecycle state
  for (const a of deployed) {
    const rec = agentRecords.find((r) => r.name === a.name);
    expect(rec, `A2A record for ${a.name}`).toBeTruthy();
    expect(String(rec!.type ?? rec!.record_type)).toMatch(/A2A|AGENT/);
    expect(["PENDING_APPROVAL", "APPROVED", "DRAFT"]).toContain(String(rec!.status));
  }

  // import the skill bundle (idempotent: a previous run may have left the record)
  let recordId = (records.find((r) => r.name === "expense-report-writer")?.record_id as string | undefined) ?? "";
  if (!recordId) {
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "n10-skill-"));
    const zip = path.join(tmp, "expense-report-writer.zip");
    execSync(`zip -qr "${zip}" expense-report-writer`, { cwd: path.join(REPO_ROOT, "samples/skills") });
    await page.goto("/v2/registry?view=register&type=AGENT_SKILLS");
    await page.getByTestId("v2-registry-source-zip").click();
    await page.getByTestId("v2-registry-zip-input").setInputFiles(zip);
    await expect(page.getByTestId("v2-registry-preview")).toBeVisible({ timeout: 60_000 });
    await expect(page.getByTestId("v2-registry-preview-name")).toHaveValue(/expense-report-writer/);
    await evidence(page, "case08-skill-preview");
    await page.getByTestId("v2-registry-import").click();
    await expect(page).toHaveURL(/view=detail&id=/, { timeout: 60_000 });
    recordId = new URL(page.url()).searchParams.get("id")!;
  } else {
    ledger(`case8 skill record already present (${recordId}) — skipping re-import`);
  }
  ledger(`case8 skill imported record=${recordId}`);

  // lifecycle → APPROVED (Draft → Submit for approval → Approve, through the UI)
  const status = await approveRecordViaUi(page, recordId);
  ledger(`case8 skill record status after approve flow: ${status}`);
  let rec: Record<string, unknown> = {};
  for (let i = 0; i < 20; i++) {
    rec = await (await page.request.get(`/api/registry/records/${recordId}`)).json();
    if (rec.status === "APPROVED") break;
    await page.waitForTimeout(1500);
  }
  await attach(testInfo, "skill-record.json", rec);
  await page.reload();
  await expect(page.getByText("Published").first()).toBeVisible({ timeout: 30_000 });
  await evidence(page, "case08-skill-approved");
  expect(rec.status).toBe("APPROVED");
  ledger(`case8a end PASS record=${recordId} name=${rec.name} status=${rec.status}`);
});

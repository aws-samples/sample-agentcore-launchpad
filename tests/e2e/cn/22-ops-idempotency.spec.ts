import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { attach, evidence, ledger } from "./helpers";

/**
 * Case 22 — ops idempotency (read-only from the tester's side): /api/health 200,
 * /api/capabilities == 10 features, default workspace ready; the second `make bootstrap`
 * / `cdk deploy --all` summary is supplied by the lead (N10_BOOTSTRAP_SUMMARY=<file>) and
 * must read "reused / no changes".
 */
test("case 22: /api/health 200, capabilities intact, bootstrap re-run summary all reused", async ({ page, request }, testInfo) => {
  const health = await request.get("/api/health");
  const hb = await health.json();
  ledger(`case22 health → ${health.status()} ${JSON.stringify(hb)}`);
  expect(health.status()).toBe(200);
  expect(hb.status).toBe("ok");
  expect(hb.region).toBe("cn-northwest-1");
  const caps = await (await page.request.get("/api/capabilities")).json();
  ledger(`case22 capabilities=${(caps.features as string[]).length} ${JSON.stringify(caps.features)}`);
  // the ten kept modules + `skill_lab` (N13) + `evaluation` / `optimization` (N12) = 13 on default
  expect((caps.features as string[]).length).toBe(13);
  for (const f of ["skill_lab", "evaluation", "optimization"]) expect(caps.features).toContain(f);
  const ws = await (await page.request.get("/api/workspaces")).json();
  expect(ws.workspaces.find((w: { is_default: boolean }) => w.is_default).bootstrap_status).toBe("ready");
  await page.goto("/v2");
  await expect(page.getByTestId("v2-kpi-agents")).toBeVisible();
  await evidence(page, "case22-health-overview");
  const file = process.env.N10_BOOTSTRAP_SUMMARY;
  if (!file || !fs.existsSync(file)) {
    testInfo.annotations.push({ type: "pending", description: "bootstrap/cdk re-run summary not yet supplied by the lead (N10_BOOTSTRAP_SUMMARY)" });
    ledger("case22 bootstrap summary not supplied — health/capabilities half PASS, idempotency half PENDING");
    return;
  }
  const summary = fs.readFileSync(path.resolve(file), "utf8");
  await attach(testInfo, "bootstrap-summary.txt", summary);
  // an explicitly explained code-asset update (the extractor Lambda fix shipped in the same window) is an
  // intentional change, not a re-created resource; everything else must read reused / no changes / 0 new
  const lines = summary.split("\n");
  const bad = lines.filter((l) => /\b[1-9]\d* created|CREATE_|UPDATE_|replaced|deleted/i.test(l) && !/reused|no changes|unchanged|skipp|code asset/i.test(l));
  const intentional = lines.filter((l) => /code asset/i.test(l));
  ledger(`case22 bootstrap summary lines=${lines.length} suspicious=${bad.length} intentional-code-updates=${intentional.length}`);
  expect(bad, "every stage must be reused / no changes").toEqual([]);
  expect(summary).toMatch(/no changes/i);
  expect(summary).toMatch(/0 created/);
  expect(summary).toMatch(/0 new/);
  expect(summary).toMatch(/\/api\/health 200/);
  ledger("case22 end PASS");
});

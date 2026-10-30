import { expect, test } from "@playwright/test";
import { ledger, registryRecords } from "../helpers";
import { GATEWAY_TARGET, GATEWAY_TOOL, record, snap, stateSet, stepGate } from "./scn";

/**
 * s02 — prepare tools (plan §3 step 2; REQUIREMENTS §4.1 cases 5 + 19, read-only half).
 * The shared AWS_IAM gateway `launchpad-gw` already carries the `hr-database` Lambda target
 * (bootstrap registers + approves it). The operator confirms the tool is attachable before the
 * agents of s04 mount it: Registry record APPROVED, `GET /api/tools` lists
 * `hr-database___get_employee`, the attachables catalog marks the gateway attachable, and the
 * Gateway governance page renders the tool list. Target add/delete (case 19) is a module test,
 * not part of the storyline — see the coverage matrix.
 */
test("s02 — hr-database gateway target is APPROVED, listed in /api/tools and attachable; governance page renders", async ({ page }, testInfo) => {
  stepGate("s02");
  const records = await registryRecords(page.request);
  const hr = records.find((r) => r.name === GATEWAY_TARGET);
  expect(hr, `${GATEWAY_TARGET} registry record`).toBeTruthy();
  expect(hr!.status).toBe("APPROVED");

  const toolsRes = await page.request.get("/api/tools", { timeout: 60_000 });
  expect(toolsRes.ok(), `GET /api/tools → ${toolsRes.status()}`).toBeTruthy();
  const toolsBody = (await toolsRes.json()) as Record<string, unknown>;
  const names = JSON.stringify(toolsBody).match(/hr-database___[a-z_]+/g) ?? [];
  const tools = Array.from(new Set(names));
  await record(testInfo, "tools.json", toolsBody, `${tools.length} hr-database tools: ${tools.join(",")}`);
  expect(tools, `${GATEWAY_TOOL} offered by the gateway`).toContain(GATEWAY_TOOL);

  const attachables = (await (await page.request.get("/api/registry/attachables?refresh=true", { timeout: 60_000 })).json()) as Record<string, unknown>;
  await record(testInfo, "attachables.json", attachables, `keys=${Object.keys(attachables).join(",")}`);
  const gwEntries = JSON.stringify(attachables).match(new RegExp(`"name":\\s*"${GATEWAY_TARGET}"[^}]*"attachable":\\s*(true|false)`));
  ledger(`s02 attachables ${GATEWAY_TARGET} → ${gwEntries?.[1] ?? "not found"}`);
  expect(gwEntries?.[1], `${GATEWAY_TARGET} listed attachable`).toBe("true");

  // the Gateway hub selects its tab with `?tab=tools` (Governance.tsx); the tools list renders one LinkButton per tool
  await page.goto("/v2/governance?tab=tools");
  await expect(page.getByTestId("v2-governance-tools")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId(`v2-governance-tool-${GATEWAY_TOOL}`), `${GATEWAY_TOOL} rendered in the Tools tab`).toBeVisible({ timeout: 60_000 });
  await snap(page, "s02-gateway-tools", tools.join(","));
  await page.goto(`/v2/registry?view=detail&id=${encodeURIComponent(hr!.record_id)}`);
  await snap(page, "s02-registry-hr-database");

  stateSet("gateway", { record_id: hr!.record_id, tools });
  ledger(`s02 PASS record=${hr!.record_id} tools=${tools.length}`);
});

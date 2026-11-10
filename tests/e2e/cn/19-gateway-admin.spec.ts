import { expect, test } from "@playwright/test";
import { attach, evidence, ledger, sleep } from "./helpers";

const SCHEMA = (title: string) => JSON.stringify({
  openapi: "3.0.3",
  info: { title, version: "1.0" },
  servers: [{ url: "https://e2e2.example.invalid/api" }],
  paths: {
    "/ping": {
      get: {
        operationId: "e2e2_ping",
        summary: "N10 v2 probe operation",
        responses: { "200": { description: "pong", content: { "application/json": { schema: { type: "object" } } } } },
        security: [{ api_key: [] }],
      },
    },
  },
  components: { securitySchemes: { api_key: { type: "apiKey", in: "header", name: "Authorization" } } },
}, null, 2);

/** Case 19 — Gateway admin: add an OpenAPI target → its tool appears in /api/tools → delete → gone. */
test("case 19: add gateway target → tool listed in /api/tools → delete → tool gone", async ({ page }, testInfo) => {
  const name = `e2e2-target-${new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "")}`;
  ledger(`case19 start target=${name}`);
  // leftovers of an earlier run: delete any e2e2-target-* through the UI first
  await page.goto("/v2/connections?view=targets");
  await expect(page.getByTestId("v2-identity-targets")).toBeVisible({ timeout: 30_000 });
  const leftovers = (((await (await page.request.get("/api/identity/gateway-targets")).json()).targets ?? []) as Array<{ name: string }>).filter((t) => t.name.startsWith("e2e2-target-"));
  for (const l of leftovers) {
    const btn = page.getByTestId(`v2-target-delete-${l.name}`);
    if (await btn.isVisible({ timeout: 10_000 }).catch(() => false)) {
      await btn.click();
      await page.getByTestId("v2-confirm-ok").click();
      ledger(`case19 leftover target ${l.name} delete requested`);
      await sleep(5000);
    }
  }
  if (leftovers.length) {
    for (let i = 0; i < 30; i++) {
      const tools = ((await (await page.request.get("/api/tools?refresh=true")).json()).tools as Array<{ name: string }>).map((t) => t.name);
      if (!tools.some((t) => t.startsWith("e2e2-target-"))) break;
      await sleep(10_000);
    }
    await page.reload();
    await expect(page.getByTestId("v2-identity-targets")).toBeVisible({ timeout: 30_000 });
  }
  const toolsBefore = ((await (await page.request.get("/api/tools?refresh=true")).json()).tools as Array<{ name: string }>).map((t) => t.name);
  await evidence(page, "case19-targets-before");
  await page.getByTestId("v2-target-new").click();
  await expect(page.getByTestId("v2-target-create")).toBeVisible();
  await page.getByTestId("v2-target-name").fill(name);
  await page.getByTestId("v2-target-schema").fill(SCHEMA(name));
  await page.getByTestId("v2-target-connection").click();
  await page.getByTestId("v2-select-popup").getByRole("option", { name: /launchpad-global-bedrock/ }).click();
  await evidence(page, "case19-target-form");
  const createRes = page.waitForResponse((r) => r.url().includes("/api/identity/gateway-targets") && r.request().method() === "POST");
  await page.getByTestId("v2-target-create-submit").click();
  const cr = await createRes;
  const created = await cr.json().catch(async () => ({ raw: await cr.text() }));
  ledger(`case19 create → ${cr.status()} ${JSON.stringify(created).slice(0, 240)}`);
  await attach(testInfo, "target-created.json", created);
  expect([200, 201]).toContain(cr.status());

  // the new target's tool shows up in /api/tools (gateway sync may take a moment)
  let tools: string[] = [];
  const t0 = Date.now();
  while (Date.now() - t0 < 5 * 60 * 1000) {
    tools = ((await (await page.request.get("/api/tools?refresh=true")).json()).tools as Array<{ name: string }>).map((t) => t.name);
    if (tools.some((t) => t.startsWith(`${name}___`))) break;
    await sleep(10_000);
  }
  const mine = tools.filter((t) => t.startsWith(`${name}___`));
  ledger(`case19 tools after create (+${Math.round((Date.now() - t0) / 1000)}s): ${mine.join(",")} (before=${toolsBefore.length}, after=${tools.length})`);
  await page.reload();
  await expect(page.getByTestId("v2-identity-targets").getByText(name)).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case19-target-listed");
  await page.goto("/v2/governance?tab=tools");
  await expect(page.getByTestId("v2-governance-tools")).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case19-tools-with-target");
  expect(mine, "new target's tool in /api/tools").toContain(`${name}___e2e2_ping`);

  // ── delete ────────────────────────────────────────────────────────────────
  await page.goto("/v2/connections?view=targets");
  await expect(page.getByTestId(`v2-target-delete-${name}`)).toBeVisible({ timeout: 60_000 });
  const delRes = page.waitForResponse((r) => r.url().includes("/api/identity/gateway-targets/") && r.request().method() === "DELETE");
  await page.getByTestId(`v2-target-delete-${name}`).click();
  await page.getByTestId("v2-confirm-ok").click();
  const dr = await delRes;
  ledger(`case19 delete → ${dr.status()}`);
  expect(dr.status()).toBe(200);
  const t1 = Date.now();
  while (Date.now() - t1 < 5 * 60 * 1000) {
    tools = ((await (await page.request.get("/api/tools?refresh=true")).json()).tools as Array<{ name: string }>).map((t) => t.name);
    if (!tools.some((t) => t.startsWith(`${name}___`))) break;
    await sleep(10_000);
  }
  ledger(`case19 tools after delete (+${Math.round((Date.now() - t1) / 1000)}s): ${tools.length} mine=${tools.filter((t) => t.startsWith(`${name}___`)).length}`);
  await page.reload();
  await expect(page.getByTestId("v2-identity-targets").getByText(name)).toHaveCount(0, { timeout: 60_000 });
  await evidence(page, "case19-target-deleted");
  expect(tools.some((t) => t.startsWith(`${name}___`)), "tool gone after delete").toBe(false);
  expect(tools.sort()).toEqual([...toolsBefore].sort());
  ledger("case19 end PASS");
});

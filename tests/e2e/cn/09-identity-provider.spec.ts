import { expect, test } from "@playwright/test";
import { evidence, ledger, sleep } from "./helpers";

/** Case 9 — the Identity (Connections) page lists the `launchpad-global-bedrock` API-key provider. */
test("case 9: Identity page lists the launchpad-global-bedrock API-key provider", async ({ page }) => {
  ledger("case9 start");
  const api = await (await page.request.get("/api/identity/connections")).json();
  const row = api.connections.find((c: { name: string }) => c.name === "launchpad-global-bedrock");
  expect(row, "API: provider missing").toBeTruthy();
  expect(row.kind).toBe("api_key");
  expect(row.status).toBe("ready");
  expect(row.arn).toMatch(/^arn:aws-cn:bedrock-agentcore:cn-northwest-1:\d+:token-vault\/default\/apikeycredentialprovider\/launchpad-global-bedrock$/);

  await page.goto("/v2/connections");
  const table = page.getByTestId("v2-connections");
  await expect(table).toBeVisible();
  await expect(table.getByText("launchpad-global-bedrock", { exact: false }).first()).toBeVisible();
  await evidence(page, "case09-identity-provider");
  ledger(`case9 provider ok arn=${row.arn}`);

  // ── custom OAuth2 provider (fake endpoints) → listed → deleted → gone ──
  const name = `e2e2-oauth-${new Date().toISOString().slice(5, 16).replace(/[-T:]/g, "")}`;
  await page.getByTestId("v2-conn-new").click();
  await expect(page.getByTestId("v2-conn-create")).toBeVisible();
  await page.getByTestId("v2-conn-template-custom_endpoints").click();
  await page.getByTestId("v2-conn-field-name").fill(name);
  await page.getByTestId("v2-conn-field-client_id").fill("e2e2-client");
  await page.getByTestId("v2-conn-field-client_secret").fill("e2e2-not-a-real-secret");
  await page.getByTestId("v2-conn-field-issuer").fill("https://idp.e2e2.example.com");
  await page.getByTestId("v2-conn-field-authorization_endpoint").fill("https://idp.e2e2.example.com/oauth2/authorize");
  await page.getByTestId("v2-conn-field-token_endpoint").fill("https://idp.e2e2.example.com/oauth2/token");
  await page.getByTestId("v2-conn-field-scopes").fill("openid");
  await evidence(page, "case09-oauth2-create-form");
  const createRes = page.waitForResponse((r) => r.url().includes("/api/identity/connections/oauth2") && r.request().method() === "POST");
  await page.getByTestId("v2-conn-create-submit").click();
  const created = await createRes;
  ledger(`case9 oauth2 create → ${created.status()} ${(await created.text()).slice(0, 200)}`);
  expect([200, 201], "OAuth2 provider create").toContain(created.status());
  const cb = page.getByTestId("v2-conn-callback");
  if (await cb.isVisible({ timeout: 3000 }).catch(() => false)) await page.getByTestId("v2-conn-callback").getByRole("button").last().click();
  await expect(table.getByText(name, { exact: false }).first()).toBeVisible({ timeout: 30_000 });
  let list = await (await page.request.get("/api/identity/connections")).json();
  const mine = list.connections.find((c: { name: string }) => c.name === name);
  expect(mine, "new provider listed").toBeTruthy();
  expect(mine.kind).toBe("oauth2");
  await evidence(page, "case09-oauth2-listed");

  const delRes = page.waitForResponse((r) => r.url().includes(`/api/identity/connections/oauth2/${name}`) && r.request().method() === "DELETE");
  await page.getByTestId(`v2-conn-delete-${name}`).click();
  await page.getByTestId("v2-confirm-ok").click();
  const deleted = await delRes;
  ledger(`case9 oauth2 delete → ${deleted.status()}`);
  expect(deleted.status()).toBe(200);
  for (let i = 0; i < 10; i++) {
    list = await (await page.request.get("/api/identity/connections")).json();
    if (!list.connections.some((c: { name: string }) => c.name === name)) break;
    await sleep(2000);
  }
  expect(list.connections.some((c: { name: string }) => c.name === name), "deleted provider no longer listed").toBe(false);
  await page.reload();
  await expect(page.getByTestId("v2-connections")).toBeVisible();
  await expect(page.getByTestId("v2-connections").getByText(name, { exact: false })).toHaveCount(0);
  await evidence(page, "case09-oauth2-deleted");
  ledger(`case9 end PASS provider=${row.arn} oauth2=${name} created+deleted`);
});

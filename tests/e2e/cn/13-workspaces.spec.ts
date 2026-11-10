import { expect, test } from "@playwright/test";
import { attach, evidence, ledger } from "./helpers";

/** Case 13 — Workspaces: default workspace detail shows bootstrap ready; an invalid cross-account role ARN is rejected (4xx + UI error). */
test("case 13: default workspace ready; invalid role ARN rejected with 4xx and a visible error", async ({ page }, testInfo) => {
  ledger("case13 start");
  const ws = await (await page.request.get("/api/workspaces")).json();
  const def = ws.workspaces.find((w: { is_default: boolean }) => w.is_default);
  await attach(testInfo, "workspaces.json", ws);
  expect(def.bootstrap_status).toBe("ready");
  expect(def.region).toBe("cn-northwest-1");
  await page.goto(`/v2/workspaces?view=detail&id=${encodeURIComponent(def.id)}`);
  await expect(page.getByTestId("v2-ws-bootstrap-card").or(page.getByText(/ready/i).first())).toBeVisible({ timeout: 30_000 });
  const body = await page.locator("main, .v2-main, body").first().innerText();
  expect(body).toMatch(/ready|就绪/i);
  await evidence(page, "case13-default-workspace-detail");

  // a previous run may have left a registered-only `e2e2-bad` row: purge it through the UI first
  const purgeBad = async () => {
    const rows = (await (await page.request.get("/api/workspaces")).json()).workspaces as Array<{ id: string }>;
    if (!rows.some((w) => w.id === "e2e2-bad")) return;
    await page.goto("/v2/workspaces?view=detail&id=e2e2-bad");
    const purge = page.getByTestId("v2-ws-purge").or(page.getByTestId("v2-ws-detach"));
    await expect(purge.first()).toBeVisible({ timeout: 30_000 });
    await purge.first().click();
    const ok = page.getByTestId("v2-confirm-ok");
    if (await ok.isVisible({ timeout: 3000 }).catch(() => false)) await ok.click();
    await page.waitForTimeout(2000);
    ledger(`case13 purged leftover workspace e2e2-bad → present=${((await (await page.request.get("/api/workspaces")).json()).workspaces as Array<{ id: string }>).some((w) => w.id === "e2e2-bad")}`);
  };
  await purgeBad();

  // ── invalid cross-account config: API returns 4xx ─────────────────────────
  const badArn = "arn:aws-cn:iam::123456789012:user/not-a-role"; // a user ARN, not a role ARN
  const pre = await page.request.post("/api/workspaces/preflight", {
    data: { account_id: "123456789012", region: "cn-north-1", role_arn: "not-an-arn", external_id: "e2e2" },
  });
  ledger(`case13 preflight(bad arn) → ${pre.status()} ${(await pre.text()).slice(0, 200)}`);
  expect(pre.status()).toBeGreaterThanOrEqual(400);
  expect(pre.status()).toBeLessThan(500);
  const mismatch = await page.request.post("/api/workspaces", {
    data: { id: "e2e2-bad", name: "e2e2 bad", account_id: "123456789012", region: "cn-north-1", role_arn: "arn:aws-cn:iam::999999999999:role/other", external_id: "e2e2" },
  });
  const mm = await mismatch.json();
  ledger(`case13 create(role account mismatch) → ${mismatch.status()} ${JSON.stringify(mm).slice(0, 200)}`);
  expect(mismatch.status()).toBe(400);
  expect(String(mm.code)).toMatch(/workspace\./);
  expect((await (await page.request.get("/api/workspaces")).json()).workspaces.some((w: { id: string }) => w.id === "e2e2-bad")).toBe(false);

  // ── the same through the UI: the error is shown, nothing is created ──────
  await page.goto("/v2/workspaces?view=new");
  await page.getByTestId("v2-ws-id").fill("e2e2-bad");
  await page.getByTestId("v2-ws-name").fill("e2e2 bad workspace");
  await page.getByTestId("v2-ws-mode-external").click();
  await page.getByTestId("v2-ws-account").fill("123456789012");
  const regionInput = page.getByTestId("v2-ws-region");
  if (await regionInput.isVisible().catch(() => false)) await regionInput.fill("cn-north-1");
  await page.getByTestId("v2-ws-role-arn").fill(badArn);
  await page.getByTestId("v2-ws-external-id").fill("e2e2-external");
  await page.getByTestId("v2-ws-submit").click();
  await expect(page.getByTestId("v2-ws-create-error")).toBeVisible({ timeout: 30_000 });
  const uiError = (await page.getByTestId("v2-ws-create-error").innerText()).trim();
  ledger(`case13 UI error: ${uiError}`);
  expect(uiError.length).toBeGreaterThan(5);
  await evidence(page, "case13-invalid-role-arn-error");
  const leaked = (await (await page.request.get("/api/workspaces")).json()).workspaces.some((w: { id: string }) => w.id === "e2e2-bad");
  if (leaked) await purgeBad();
  expect(leaked, "no workspace row created from the rejected form").toBe(false);
  ledger("case13 end PASS");
});

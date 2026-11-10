import { expect, test } from "@playwright/test";
import { activeAgent, attach, chatOnce, evidence, EVIDENCE_DIR, ledger, looksLikeError } from "./helpers";

/** Case 14 — public /v1: create an API key in the Chat inspector → X-Api-Key invoke 200 (same invoke chain as Chat) → disable → 401. */
test("case 14: API key → /v1 invoke 200 with model text → disabled key → 401", async ({ page, request }, testInfo) => {
  const a = await activeAgent(page.request, "harness");
  expect(a, "case 2 agent required").toBeTruthy();
  ledger(`case14 start agent=${a!.name}`);
  await page.goto(`/v2/chat?agent=${encodeURIComponent(a!.id)}`);
  await page.getByRole("tab", { name: /API/i }).click();
  await expect(page.getByTestId("api-panel")).toBeVisible();
  const before = await page.getByTestId("api-key-row").count();
  await page.getByTestId("new-key").click();
  await expect(page.getByTestId("new-key-value")).toBeVisible({ timeout: 30_000 });
  const key = (await page.getByTestId("new-key-value").innerText()).trim();
  expect(key).toMatch(/^lp_live_[0-9a-f]{32}$/);
  await expect(page.getByTestId("api-key-row")).toHaveCount(before + 1);
  // evidence: mask the one-time key
  await page.screenshot({ path: `${EVIDENCE_DIR}/case14-api-key-created.png`, fullPage: true, mask: [page.getByTestId("new-key-value")] });

  const prompt = "Public API probe: what is the secret phrase? Reply with it and nothing else.";
  const v1 = await request.post(`/v1/agents/${a!.id}/invoke`, { headers: { "X-Api-Key": key }, data: { prompt } });
  const v1Body = await v1.json().catch(async () => ({ raw: await v1.text() }));
  ledger(`case14 /v1 invoke → ${v1.status()} ${JSON.stringify(v1Body).slice(0, 240)}`);
  await attach(testInfo, "v1-invoke.json", v1Body);
  expect(v1.status()).toBe(200);
  expect(looksLikeError(String(v1Body.text))).toBe(false);
  expect(String(v1Body.text)).toMatch(/LAUNCHPAD-CN-OK/i);
  expect(v1Body.session_id).toBeTruthy();
  // same path as Chat: the console answers the identical prompt with the same phrase
  const chat = await chatOnce(page, a!.id, prompt);
  expect(chat.answer).toMatch(/LAUNCHPAD-CN-OK/i);
  // the /v1 session is listed with the console sessions (one invoke chain, one ledger)
  const sessions = (await (await page.request.get(`/api/chat/${a!.id}/sessions`)).json()).sessions as Array<{ session_id: string }>;
  ledger(`case14 /v1 session listed=${sessions.some((s) => s.session_id === v1Body.session_id)}`);
  const list = await request.get("/v1/agents", { headers: { "X-Api-Key": key } });
  expect(list.status()).toBe(200);
  expect(((await list.json()).agents as Array<{ id: string }>).some((x) => x.id === a!.id)).toBe(true);

  // no key / bad key
  expect((await request.post(`/v1/agents/${a!.id}/invoke`, { data: { prompt } })).status()).toBe(401);
  expect((await request.post(`/v1/agents/${a!.id}/invoke`, { headers: { "X-Api-Key": "lp_live_deadbeef" }, data: { prompt } })).status()).toBe(401);

  // ── disable in the UI → 401 ──────────────────────────────────────────────
  await page.goto(`/v2/chat?agent=${encodeURIComponent(a!.id)}`);
  await page.getByRole("tab", { name: /API/i }).click();
  const row = page.getByTestId("api-key-row").filter({ hasText: key.slice(0, 12) }).first();
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: /disable/i }).click();
  await page.getByTestId("v2-confirm-ok").click();
  await expect(row.getByText(/disabled/i)).toBeVisible({ timeout: 30_000 });
  await evidence(page, "case14-api-key-disabled");
  const after = await request.post(`/v1/agents/${a!.id}/invoke`, { headers: { "X-Api-Key": key }, data: { prompt } });
  ledger(`case14 /v1 after disable → ${after.status()} ${(await after.text()).slice(0, 160)}`);
  expect(after.status()).toBe(401);
  ledger(`case14 end PASS agent=${a!.id}`);
});

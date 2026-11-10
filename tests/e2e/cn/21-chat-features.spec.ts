import { expect, test } from "@playwright/test";
import { activeAgent, attach, chatOnce, evidence, ledger } from "./helpers";

/** Case 21 — Chat: END SESSION → next prompt gets a new session id; New session; interruption mid-stream leaves no hanging UI state. */
test("case 21: END SESSION → new id; New session; mid-stream interruption → no hanging state", async ({ page }, testInfo) => {
  const a = await activeAgent(page.request, "harness");
  expect(a, "case 2 agent required").toBeTruthy();
  ledger(`case21 start agent=${a!.name}`);

  // ── END SESSION ──────────────────────────────────────────────────────────
  const s1 = await chatOnce(page, a!.id, "Chat-features probe 1: reply with one word.");
  const stopRes = page.waitForResponse((r) => r.url().includes(`/sessions/${s1.sessionId}/stop`) && r.request().method() === "POST");
  await page.getByTestId("end-session").click();
  await page.getByTestId("v2-confirm-ok").click();
  const sr = await stopRes;
  const stop = await sr.json();
  ledger(`case21 end-session → ${sr.status()} ${JSON.stringify(stop)}`);
  expect(sr.status()).toBe(200);
  expect(stop.ended || stop.already_ended).toBe(true);
  await expect(page).not.toHaveURL(/session=/);
  await evidence(page, "case21-after-end-session");
  const s2 = await chatOnce(page, a!.id, "Chat-features probe 2: reply with one word.");
  ledger(`case21 after END: s1=${s1.sessionId} s2=${s2.sessionId}`);
  expect(s2.sessionId).not.toBe(s1.sessionId);
  const sessions = (await (await page.request.get(`/api/chat/${a!.id}/sessions`)).json()).sessions as Array<{ session_id: string; ended_at: string | null }>;
  expect(sessions.find((s) => s.session_id === s1.sessionId)?.ended_at).toBeTruthy();

  // ── New session button ───────────────────────────────────────────────────
  await page.getByTestId("new-session").click();
  await expect(page).not.toHaveURL(/session=/);
  await expect(page.locator(".v2-chat-msg")).toHaveCount(0);
  const s3 = await chatOnce(page, a!.id, "Chat-features probe 3: reply with one word.");
  expect(s3.sessionId).not.toBe(s2.sessionId);
  await evidence(page, "case21-new-session");

  // ── interruption: reload while the answer streams ────────────────────────
  await page.getByTestId("chat-input").fill("Write a 300-word story about a lighthouse keeper, slowly, paragraph by paragraph.");
  await page.getByTestId("chat-send").click();
  await expect(page.locator(".v2-chat-msg.user").last()).toBeVisible();
  await page.waitForTimeout(2500);
  const streamingBefore = await page.getByText(/streaming/i).count();
  await page.reload();
  await expect(page.getByTestId("chat-input")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("chat-input")).toBeEnabled({ timeout: 60_000 });
  await expect(page.getByText(/streaming/i)).toHaveCount(0);
  await expect(page.locator(".v2-chat-caret")).toHaveCount(0);
  await evidence(page, "case21-after-interrupt");
  // the console still works: one more normal turn in the same (restored) session
  const s4 = await chatOnce(page, a!.id, "Interrupted earlier — reply with one word.", { sessionId: s3.sessionId });
  await attach(testInfo, "post-interrupt-answer.txt", s4.answer);
  ledger(`case21 interrupt: streamingBefore=${streamingBefore} post-interrupt turn ok (${s4.seconds}s) errors=${s4.errors}`);
  expect(s4.errors).toBe(0);
  expect(s4.answer.length).toBeGreaterThan(0);
  ledger(`case21 end PASS s1=${s1.sessionId} s2=${s2.sessionId} s3=${s3.sessionId}`);
});

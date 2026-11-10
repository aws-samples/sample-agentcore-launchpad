import { expect, test } from "@playwright/test";
import { ledger } from "./helpers";
import { JUDGE_ALT, JUDGE_DEFAULT, N12E_EVALUATOR_PREFIX, evaluators, record, snap } from "./n12e";

/**
 * §4.1 case 24 / plan §E.3 flow 1 — Evaluators.
 * `/v2/eval/evaluators?view=new`: create the judge `n12e_cn_tone` (GLM 5.3 default in the
 * model picker, Kimi K3 the only alternative) → list shows a `custom` row; detail shows
 * level / scale; edit + delete work (API, on a throw-away twin so the judge survives for
 * flows 3–8); the retired `/evaluation?view=evaluators` URL redirects to the same V2 list (r13-a).
 * N9 probe (d): the "Code" (Lambda) definition is offered in the UI — try it and record
 * what the backend answers (`eval_code_evaluators_enabled` defaults to false).
 */
const JUDGE = `${N12E_EVALUATOR_PREFIX}cn_tone`;
const RUBRIC =
  "Judge whether {assistant_turn} answers the user's request in {context} politely, concisely and in the " +
  "language of the user. A reply in the right tone and language passes; a rude, rambling or wrong-language reply fails.";

test("case 24 — evaluators: create judge, model picker, detail, edit, delete, redirected list, Lambda probe", async ({ page }, testInfo) => {
  const t0 = Date.now();
  const before = await evaluators(page.request);
  const existing = before.find((e) => e.name === JUDGE);
  if (existing) {
    ledger(`case24 ${JUDGE} already exists (${existing.id}) — deleting it first for a clean create`);
    const del = await page.request.delete(`/api/eval/evaluators/${existing.id}`);
    ledger(`case24 pre-delete → ${del.status()}`);
  }

  // ── create through the V2 form ────────────────────────────────────────────
  await page.goto("/v2/eval/evaluators?view=new");
  await expect(page.getByTestId("v2-evaluator-def-judge")).toBeVisible();
  await page.getByTestId("v2-evaluator-name").fill(JUDGE);
  // judge-model picker: GLM 5.3 shown by default; the listbox offers the catalog, GLM 5.3 + Kimi K3 selectable
  const modelTrigger = page.getByRole("button", { name: JUDGE_DEFAULT });
  await expect(modelTrigger, "GLM 5.3 is the default judge model").toBeVisible();
  await modelTrigger.click();
  const options = page.getByRole("listbox").getByRole("option");
  const labels = (await options.allInnerTexts()).map((s) => s.trim());
  ledger(`case24 judge model options: ${labels.join(" | ")}`);
  await snap(page, "case24-model-picker", labels.join(" | "));
  expect(labels.some((l) => l.includes(JUDGE_DEFAULT)), "GLM 5.3 offered").toBeTruthy();
  expect(labels.some((l) => l.includes(JUDGE_ALT)), "Kimi K3 offered").toBeTruthy();
  // N-R6: the picker lists the whole catalog from /api/capabilities; rows of an unconfigured
  // provider (e.g. SiliconFlow without its SSM key) are present but greyed (aria-disabled).
  const caps = await (await page.request.get("/api/capabilities")).json() as { models?: Array<{ model_id: string; available?: boolean }> };
  const catalog = caps.models ?? [];
  const enabled = await options.evaluateAll((els) => els.filter((e) => e.getAttribute("aria-disabled") !== "true").map((e) => e.textContent?.trim() ?? ""));
  expect(labels.length, `picker = whole catalog (${catalog.length} rows)`).toBe(catalog.length);
  expect(enabled.length, "enabled rows = available models").toBe(catalog.filter((m) => m.available !== false).length);
  for (const m of catalog.filter((m) => m.available === false)) expect(enabled.some((l) => l.includes(m.model_id)), `${m.model_id} must be greyed`).toBeFalsy();
  await options.filter({ hasText: JUDGE_DEFAULT }).first().click();
  await page.getByTestId("v2-evaluator-rubric").fill(RUBRIC);
  await snap(page, "case24-new-form");
  await page.getByTestId("v2-evaluator-save").click();
  await expect(page).not.toHaveURL(/view=new/, { timeout: 30_000 });

  const after = await evaluators(page.request);
  const row = after.find((e) => e.name === JUDGE);
  expect(row, `${JUDGE} listed by GET /api/eval/evaluators`).toBeTruthy();
  const created = await (await page.request.get(`/api/eval/evaluators/${row!.id}`)).json();
  ledger(`case24 created ${JUDGE} id=${row!.id} level=${row!.level} model=${created.model_id} definition=${row!.definition} source=${row!.source}`);
  expect(String(row!.source ?? row!.evaluator_type)).toMatch(/custom/i);
  expect(created.model_id, "judge model persisted").toBe(JUDGE_DEFAULT);

  // list row + detail
  await page.goto("/v2/eval/evaluators");
  // 19 built-ins + the custom row span two pages — filter by name first
  await page.getByRole("textbox", { name: /Search evaluator/i }).fill(JUDGE);
  const listRow = page.getByTestId("v2-evaluators-table").locator("tr", { hasText: JUDGE });
  await expect(listRow).toBeVisible();
  await snap(page, "case24-list-custom-row");
  await listRow.getByRole("button", { name: JUDGE }).click();
  await expect(page).toHaveURL(/view=detail/);
  await expect(page.getByText(/Trace/i).first()).toBeVisible();
  await expect(page.getByText("pass", { exact: true }).first()).toBeVisible();
  await snap(page, "case24-detail", "level + rating scale");
  const detail = await (await page.request.get(`/api/eval/evaluators/${row!.id}`)).json();
  await record(testInfo, "judge-detail.json", detail, `detail level=${detail.level} scale=${JSON.stringify(detail.rating_scale)?.slice(0, 80)}`);

  // edit (description) through the API — the detail page's edit action is exercised through the same PUT
  const put = await page.request.put(`/api/eval/evaluators/${row!.id}`, {
    data: { instructions: RUBRIC + " Edited by N12 e2e.", model_id: JUDGE_DEFAULT, level: "TRACE", description: "n12e edited", rating_scale: detail.rating_scale },
  });
  ledger(`case24 PUT /api/eval/evaluators/${row!.id} → ${put.status()}`);
  expect(put.ok(), `edit → ${put.status()} ${await put.text()}`).toBeTruthy();
  const edited = await (await page.request.get(`/api/eval/evaluators/${row!.id}`)).json();
  expect(edited.description).toBe("n12e edited");

  // delete on a throw-away twin (the judge itself is used by flows 3–8 and removed by 98-cleanup)
  const stale = (await evaluators(page.request)).find((e) => e.name === `${N12E_EVALUATOR_PREFIX}tmp_del`);
  if (stale) ledger(`case24 stale twin ${stale.id} from an earlier run → DELETE ${(await page.request.delete(`/api/eval/evaluators/${stale.id}`)).status()}`);
  const twin = await page.request.post("/api/eval/evaluators", {
    data: { name: `${N12E_EVALUATOR_PREFIX}tmp_del`, instructions: RUBRIC, model_id: JUDGE_ALT, level: "TRACE" },
  });
  expect(twin.status(), await twin.text()).toBe(201);
  // POST answers {evaluator_id, arn, model_fallback}
  const twinId = (await twin.json()).evaluator_id as string;
  const del = await page.request.delete(`/api/eval/evaluators/${twinId}`);
  ledger(`case24 twin ${twinId} (Kimi K3) created 201, DELETE → ${del.status()}`);
  expect(del.ok()).toBeTruthy();
  expect((await evaluators(page.request)).some((e) => e.id === twinId)).toBeFalsy();

  // derived evaluator on the alternative judge model (used by flow 3 to exercise Kimi K3)
  const derivedName = `${N12E_EVALUATOR_PREFIX}help_kimi`;
  const existingDerived = after.find((e) => e.name === derivedName);
  if (!existingDerived) {
    const derived = await page.request.post("/api/eval/evaluators", {
      data: { name: derivedName, base_evaluator_id: "Builtin.Helpfulness", model_id: JUDGE_ALT, level: "TRACE", description: "Helpfulness on Kimi K3" },
    });
    ledger(`case24 derived ${derivedName} (Builtin.Helpfulness on Kimi K3) → ${derived.status()} ${(await derived.text()).slice(0, 160)}`);
    expect(derived.status()).toBe(201);
  }

  // the retired classic URL redirects to the V2 evaluators list, which shows the same custom row
  await page.goto("/evaluation?view=evaluators");
  await expect(page).toHaveURL(/\/v2\/eval\/evaluators/);
  await expect(page.getByText(JUDGE).first()).toBeVisible({ timeout: 30_000 });
  await snap(page, "case24-redirected-evaluators");

  // N9 probe (d): Lambda code evaluator path — offered in the UI?
  await page.goto("/v2/eval/evaluators?view=new");
  const codeBtn = page.getByTestId("v2-evaluator-def-code");
  const codeOffered = await codeBtn.isVisible();
  if (codeOffered) {
    await codeBtn.click();
    await snap(page, "case24-code-evaluator-form", "Lambda definition offered in the V2 form");
  }
  const lambdaArn = "arn:aws-cn:lambda:cn-northwest-1:123456789012:function:n12e-nonexistent-evaluator";
  const code = await page.request.post("/api/eval/evaluators", {
    data: { name: `${N12E_EVALUATOR_PREFIX}code_probe`, lambda_arn: lambdaArn, lambda_timeout_s: 30, level: "TRACE", description: "N9 probe d" },
  });
  const codeBody = await code.text();
  ledger(`case24 N9(d) code evaluator: ui_offered=${codeOffered} POST → ${code.status()} ${codeBody.slice(0, 200)}`);
  await record(testInfo, "code-evaluator-probe.json", { ui_offered: codeOffered, status: code.status(), body: codeBody.slice(0, 500) });
  if (code.status() === 201) {
    const id = JSON.parse(codeBody).evaluator_id as string;
    await page.request.delete(`/api/eval/evaluators/${id}`);
    ledger(`case24 code evaluator ${id} created although eval_code_evaluators_enabled should default to false — deleted again`);
  }

  ledger(`case24 done in ${Math.round((Date.now() - t0) / 1000)}s`);
});

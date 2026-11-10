import { expect, test } from "@playwright/test";
import { e2eName, ledger } from "./helpers";
import { datasets, record, snap } from "./n12e";

/**
 * §4.1 case 25 / plan §E.3 flow 2 — Dataset.
 * `/v2/eval/data?view=dataset-new`: a 3-item predefined dataset (one item with an
 * `expected_response`, one with `assertions` — the V2 form only offers input + expected
 * output, so the assertions are added through `PUT /api/eval/datasets/{id}`; the classic
 * scenario editor at `/evaluation?view=datasets` has the field). Then SYNC (snapshot
 * `ACTIVE`) → PUBLISH VERSION → version `1` listed on the cloud page.
 */
// Every item asks for the calculator so `Builtin.TrajectoryInOrderMatch` has a reference
// trajectory (the task wizard refuses that evaluator when a scenario lacks
// `expected_tool_trajectory`); the Strands template agent exposes a `calculator` tool.
export const ITEMS = [
  { input: "Use your calculator tool to compute 17*23 and reply with the number only.", expected: "391" },
  { input: "Use your calculator tool: what is 1000 minus 358? Reply with the number and one short sentence.", expected: "" },
  { input: "Use your calculator tool to compute the square root of 144 and say what it is in one sentence.", expected: "" },
];
export const EXPECTED_TRAJECTORY = ["calculator"];

test("case 25 — dataset: V2 create (3 items) → assertions → SYNC → PUBLISH VERSION → cloud listing", async ({ page }, testInfo) => {
  const t0 = Date.now();
  const name = e2eName("ds");

  await page.goto("/v2/eval/data?view=dataset-new");
  await page.getByTestId("v2-dataset-name").fill(name);
  await page.getByPlaceholder(/description/i).first().fill("N12 e2e predefined scenarios (3 items; 1 expected_response, 1 assertions)").catch(() => {});
  await page.getByRole("button", { name: "Add record" }).click();
  await page.getByRole("button", { name: "Add record" }).click();
  for (const [i, it] of ITEMS.entries()) {
    await page.getByTestId(`v2-dataset-input-${i}`).fill(it.input);
    if (it.expected) await page.getByRole("textbox", { name: `Expected output ${i + 1}` }).fill(it.expected);
  }
  await snap(page, "case25-dataset-new-form");
  await page.getByTestId("v2-dataset-save").click();
  await expect(page).not.toHaveURL(/view=dataset-new/, { timeout: 30_000 });

  let row = (await datasets(page.request)).find((d) => d.name === name);
  expect(row, `${name} listed by GET /api/eval/datasets`).toBeTruthy();
  ledger(`case25 created ${name} id=${row!.id} items=${(row!.items ?? []).length} kind=${row!.kind ?? "?"}`);
  await record(testInfo, "dataset-created.json", row);
  expect((row!.items ?? []).length).toBe(3);

  // add assertions to item 2 (ground truth for Builtin.GoalSuccessRate) and the expected tool
  // trajectory to every item (Builtin.Trajectory*Match) — neither is offered by the V2 form
  const items = (row!.items ?? []).map((it, i) => ({
    ...it,
    // dataset item key is `expected_trajectory` (list of tool names); judges see it as {expected_tool_trajectory}
    expected_trajectory: EXPECTED_TRAJECTORY,
    ...(i === 1 ? { assertions: ["The reply states that 1000 minus 358 equals 642."] } : {}),
  }));
  const put = await page.request.put(`/api/eval/datasets/${row!.id}`, { data: { items } });
  ledger(`case25 PUT items (assertions on item 2) → ${put.status()}`);
  expect(put.ok(), await put.text()).toBeTruthy();
  row = (await datasets(page.request)).find((d) => d.id === row!.id);
  const gt = (row!.items ?? []).map((it) => ({
    expected: ((it.turns as Array<{ expected_response?: string }> | undefined)?.[0]?.expected_response ?? it.expected_response ?? "") !== "",
    assertions: Array.isArray(it.assertions) && it.assertions.length > 0,
  }));
  ledger(`case25 ground truth per item: ${JSON.stringify(gt)}`);
  expect(gt.filter((g) => g.expected).length).toBe(1);
  expect(gt.filter((g) => g.assertions).length).toBe(1);

  // V2 datasets tab shows the row
  await page.goto("/v2/eval/data?tab=datasets");
  await page.getByTestId("v2-tab-datasets").click().catch(() => {});
  await expect(page.getByText(name).first()).toBeVisible({ timeout: 30_000 });
  await snap(page, "case25-v2-datasets-list");

  // SYNC → snapshot ACTIVE
  const sync = await page.request.post(`/api/eval/datasets/${row!.id}/sync-to-aws`);
  const syncBody = await sync.json().catch(() => ({}));
  ledger(`case25 sync-to-aws → ${sync.status()} ${JSON.stringify(syncBody).slice(0, 240)}`);
  expect(sync.ok(), JSON.stringify(syncBody)).toBeTruthy();
  await record(testInfo, "sync.json", syncBody);
  // the response is the dataset row; its `cloud` block is the S3 snapshot (CN "cloud" dataset = S3 snapshot)
  const cloudBlock = (syncBody.cloud ?? {}) as { dataset_id?: string; status?: string; arn?: string; versions?: Array<{ version: string; example_count: number }> };
  const cloudId = String(cloudBlock.dataset_id ?? "");
  ledger(`case25 snapshot ${cloudId} status=${cloudBlock.status} arn=${cloudBlock.arn}`);
  expect(String(cloudBlock.status).toUpperCase(), "snapshot status").toBe("ACTIVE");
  expect(cloudBlock.arn ?? "", "snapshot lives in the CN partition artifacts bucket").toMatch(/^arn:aws-cn:s3:::launchpad-artifacts-/);

  // PUBLISH VERSION → version 1
  const pub = await page.request.post(`/api/eval/datasets/${row!.id}/publish-version`);
  const pubBody = await pub.json().catch(() => ({}));
  ledger(`case25 publish-version → ${pub.status()} versions=${JSON.stringify(pubBody.cloud?.versions)}`);
  expect(pub.ok(), JSON.stringify(pubBody)).toBeTruthy();
  await record(testInfo, "publish.json", { ...pubBody, items: undefined });
  const versions = (pubBody.cloud?.versions ?? []) as Array<{ version: string; example_count: number }>;
  expect(versions.map((v) => v.version), "first published version is 1").toEqual(["1"]);
  expect(versions[0].example_count).toBe(3);

  // cloud listing
  const cloud = await (await page.request.get("/api/eval/datasets/cloud")).json();
  await record(testInfo, "cloud-datasets.json", cloud);
  const cloudRow = (cloud.datasets as Array<Record<string, unknown>>).find((d) => String(d.datasetId ?? d.id) === cloudId);
  expect(cloudRow, `cloud snapshot ${cloudId} listed`).toBeTruthy();
  ledger(`case25 cloud row ${JSON.stringify(cloudRow).slice(0, 240)}`);
  expect(String(cloudRow!.status)).toBe("ACTIVE");
  const cloudDetail = await (await page.request.get(`/api/eval/datasets/cloud/${cloudId}`)).json();
  await record(testInfo, "cloud-detail.json", cloudDetail, `cloud detail versions=${JSON.stringify(cloudDetail.versions ?? cloudDetail.cloud?.versions)?.slice(0, 120)}`);

  // the retired classic URL redirects to the V2 data center's datasets tab, which shows the row
  await page.goto("/evaluation?view=datasets");
  await expect(page).toHaveURL(/\/v2\/eval\/data/);
  await expect(page.getByText(name).first()).toBeVisible({ timeout: 30_000 });
  await snap(page, "case25-redirected-datasets", "local row + cloud snapshot");
  await page.getByText(name).first().click().catch(() => {});
  await page.waitForTimeout(1500);
  await snap(page, "case25-redirected-dataset-detail");

  ledger(`case25 done in ${Math.round((Date.now() - t0) / 1000)}s`);
});

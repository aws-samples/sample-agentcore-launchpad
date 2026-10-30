import { expect, test } from "@playwright/test";
import { execSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { activeAgent, attach, chatOnce, createByocViaWizard, e2eName, evidence, getAgent, ledger, REPO_ROOT, waitForDeploy, waitForJob } from "./helpers";

/**
 * N10v3-bug7 (= N10v2-bug9) re-check (round 4): a FAILED redeploy of a serving agent must
 * NOT flip it to `failed` when the live version is unchanged.
 *
 * Part 1 — failure in `package` (BYOC code zip whose requirements.txt pins a package that
 * does not exist): job `failed`; the agent STAYS `active` on the same version; the API carries
 * `last_deploy_failed: true` + `last_deploy_error`; `POST /api/chat/{id}` answers (no 409);
 * the console shows the amber "REDEPLOY FAILED" tag (`v2-agent-redeploy-failed-<name>`) and
 * the detail alert; the marker clears on the next successful redeploy? (not exercised).
 *
 * Part 2 — failure in `deploy` (a main.py that exits at import, so the new runtime version
 * never reaches READY): per the review's stage gate the agent becomes `failed` (its
 * `version` may name a never-READY version). Whatever AgentCore does with a crashing
 * container (UPDATE_FAILED vs READY-but-500) is recorded, not assumed.
 */
function zipOf(mutate: (dir: string) => void, label: string): string {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), `n10v4-bug7-${label}-`));
  fs.cpSync(path.join(REPO_ROOT, "samples/byoc/hello-http"), tmp, { recursive: true });
  mutate(tmp);
  const zip = path.join(os.tmpdir(), `n10v4-bug7-${label}-${Date.now()}.zip`);
  execSync(`zip -qr "${zip}" .`, { cwd: tmp });
  return zip;
}

test("bug7: failed redeploy (package) keeps the agent active + marker + Chat 200; failed deploy (runtime start) → failed", async ({ page }, testInfo) => {
  test.setTimeout(60 * 60 * 1000);
  // ── a serving BYOC agent (case 4's, or a fresh one) ───────────────────────
  let a = await activeAgent(page.request, "byoc");
  if (!a) {
    const name = e2eName("byoc");
    const zip = zipOf(() => {}, "clean");
    const t0 = Date.now();
    const id = await createByocViaWizard(page, { name, zip, entrypoint: "main.py", evidencePrefix: "bug7" });
    const dep = await waitForDeploy(page, testInfo, id, "bug7-create", t0);
    expect(dep.job.status, `fresh BYOC deploy failed: ${dep.job.error ?? ""}`).toBe("succeeded");
    a = { id, name };
  }
  const before = await getAgent(page.request, a.id);
  const spec = before.spec as Record<string, unknown>;
  ledger(`bug7 start agent=${a.name} status=${before.status} version=${before.version} last_deploy_failed=${before.last_deploy_failed}`);
  const chat0 = await chatOnce(page, a.id, "Say OK.");
  ledger(`bug7 chat before: errors=${chat0.errors} ${chat0.answer.slice(0, 80).replace(/\n/g, " ")}`);
  expect(before.status).toBe("active");

  const redeployWith = async (zip: string, label: string) => {
    const up = await page.request.post("/api/agents/uploads", { multipart: { file: { name: `${label}.zip`, mimeType: "application/zip", buffer: fs.readFileSync(zip) } } });
    const upBody = await up.json().catch(async () => ({ raw: await up.text() }));
    ledger(`bug7 ${label} upload → ${up.status()} ${JSON.stringify(upBody).slice(0, 200)}`);
    expect(up.status(), "upload staged").toBe(201);
    const byoc = { ...(spec.byoc as Record<string, unknown>), upload_id: upBody.upload_id, provenance: undefined, entrypoint: "main.py" };
    const rd = await page.request.post(`/api/agents/${a!.id}/redeploy`, { data: { ...spec, byoc } });
    const rdBody = await rd.text();
    ledger(`bug7 ${label} redeploy → ${rd.status()} ${rdBody.slice(0, 200)}`);
    await attach(testInfo, `${label}-redeploy.txt`, `${rd.status()} ${rdBody}`);
    expect(rd.status(), "redeploy accepted").toBe(202);
    const jobId = String((JSON.parse(rdBody) as { job_id: string }).job_id);
    const t0 = Date.now();
    const job = await waitForJob(page.request, jobId, { timeoutMs: 25 * 60 * 1000, intervalMs: 10_000 });
    const errors = ((job.events as Array<{ stage: string; level?: string; msg: string }>) ?? []).filter((e) => e.level === "error" || /LIVE_VERSION_KEPT/.test(e.msg)).map((e) => `${e.stage}: ${e.msg}`);
    await attach(testInfo, `${label}-redeploy-job.json`, job);
    const after = await getAgent(page.request, a!.id);
    ledger(`bug7 ${label} job ${job.status} in ${Math.round((Date.now() - t0) / 1000)}s stage-errors: ${errors.join(" | ").slice(0, 400)}`);
    ledger(`bug7 ${label} agent after: status=${after.status} version=${after.version} last_deploy_failed=${after.last_deploy_failed} last_deploy_error=${String(after.last_deploy_error ?? "").slice(0, 200)} error=${String(after.error ?? "").slice(0, 120)}`);
    return { job, after, errors, seconds: Math.round((Date.now() - t0) / 1000) };
  };

  // ── Part 1: package-stage failure ─────────────────────────────────────────
  const brokenReq = zipOf((d) => fs.appendFileSync(path.join(d, "requirements.txt"), "\nnonexistent-package-e2e4==9.9.9\n"), "badreq");
  const p1 = await redeployWith(brokenReq, "part1-badreq");
  await page.goto("/v2/agents");
  // the V2 shell sits on its AuthGate spinner for ~5–8 s after a hard navigation (0.5 s RTT × sequential
  // auth/workspace fetches) — wait for the table, then for the tag (first run checked too early)
  await expect(page.getByTestId("v2-agents-table")).toBeVisible({ timeout: 60_000 });
  const tag = page.getByTestId(`v2-agent-redeploy-failed-${a.name}`);
  const tagVisible = await expect(tag).toBeVisible({ timeout: 30_000 }).then(() => true).catch(() => false);
  const tagTitle = tagVisible ? await tag.getAttribute("title") : null;
  await evidence(page, "bug7-agents-list-marker");
  await page.goto(`/v2/agents?view=detail&id=${encodeURIComponent(a.id)}`);
  await expect(page.getByTestId("v2-agent-log")).toBeVisible({ timeout: 60_000 });
  const detailText = (await page.locator("main, body").first().innerText()).replace(/\n/g, " ");
  const detailAlert = /REDEPLOY FAILED|last re-publish failed|previous version keeps serving/i.test(detailText);
  await evidence(page, "bug7-agent-detail-marker");
  const chatApi = await page.request.post(`/api/chat/${a.id}`, { data: { prompt: "Say OK again.", session_id: null } });
  const chatApiText = (await chatApi.text()).slice(0, 300);
  ledger(`bug7 part1 marker: list tag visible=${tagVisible} title="${String(tagTitle ?? "").slice(0, 120)}" detail alert=${detailAlert}; POST /api/chat → ${chatApi.status()} ${chatApiText.replace(/\n/g, " ").slice(0, 120)}`);
  const chat1 = await chatOnce(page, a.id, "Say OK a third time.");
  await evidence(page, "bug7-chat-after-failed-redeploy");
  ledger(`bug7 part1 console chat after failed redeploy: errors=${chat1.errors} ${chat1.answer.slice(0, 100).replace(/\n/g, " ")}`);
  await attach(testInfo, "bug7-part1-summary.json", { job_status: p1.job.status, status_before: before.status, status_after: p1.after.status, version_before: before.version, version_after: p1.after.version, last_deploy_failed: p1.after.last_deploy_failed, last_deploy_error: p1.after.last_deploy_error, chat_api_status: chatApi.status(), list_tag: tagVisible, detail_alert: detailAlert, stage_errors: p1.errors });
  expect(p1.job.status, "the package failure was injected").toBe("failed");
  expect(p1.after.status, "agent STAYS active on its live version (N10v3-bug7)").toBe("active");
  expect(Number(p1.after.version), "version unchanged").toBe(Number(before.version));
  expect(p1.after.last_deploy_failed, "API: last_deploy_failed").toBe(true);
  expect(String(p1.after.last_deploy_error ?? ""), "API: last_deploy_error carries the package error").toMatch(/requirement|resolve|nonexistent|package/i);
  expect(chatApi.status(), "POST /api/chat still 200 (no 409 agent.not_active)").toBe(200);
  expect(chat1.errors, "console Chat still answers").toBe(0);
  expect(tagVisible, "amber REDEPLOY FAILED tag in the agents list").toBe(true);
  expect(detailAlert, "detail page alert names the failed re-publish").toBe(true);

  // ── Part 2: deploy-stage failure (container exits at start) ──────────────
  const crashing = zipOf((d) => fs.writeFileSync(path.join(d, "main.py"), '"""N10 v4 bug7 part 2: a BYOC entrypoint that exits before serving :8080."""\nimport sys\n\nprint("e2e4 bug7: intentional start-up failure", flush=True)\nsys.exit(3)\n'), "crash");
  const p2 = await redeployWith(crashing, "part2-crash");
  await page.goto(`/v2/agents?view=detail&id=${encodeURIComponent(a.id)}`);
  await expect(page.getByTestId("v2-agent-log")).toBeVisible({ timeout: 60_000 });
  await evidence(page, "bug7-part2-agent-detail");
  await page.goto("/v2/agents");
  await evidence(page, "bug7-part2-agents-list");
  const chatApi2 = await page.request.post(`/api/chat/${a.id}`, { data: { prompt: "Say OK.", session_id: null } });
  const chat2Text = (await chatApi2.text()).slice(0, 300);
  ledger(`bug7 part2 POST /api/chat → ${chatApi2.status()} ${chat2Text.replace(/\n/g, " ").slice(0, 160)}`);
  await attach(testInfo, "bug7-part2-summary.json", { job_status: p2.job.status, status_after: p2.after.status, version_after: p2.after.version, last_deploy_failed: p2.after.last_deploy_failed, error: p2.after.error, stage_errors: p2.errors, chat_api_status: chatApi2.status(), chat_api_text: chat2Text, seconds: p2.seconds });
  const failedStage = p2.errors.map((e) => e.split(":")[0]).find((s) => ["generate", "package", "provision", "deploy", "register"].includes(s)) ?? "n/a";
  ledger(`bug7 part2 verdict: job=${p2.job.status} failed-stage=${failedStage} agent=${p2.after.status} version=${before.version}→${p2.after.version}`);
  if (p2.job.status === "failed" && failedStage === "deploy") {
    expect(p2.after.status, "a failure inside `deploy` (never-READY version) → agent failed (review stage gate)").toBe("failed");
  } else if (p2.job.status === "succeeded") {
    ledger("bug7 part2 NOTE: AgentCore accepted the crashing container (runtime READY) — the start-up failure surfaces only at invoke time; recorded, no stage-gate assertion possible");
  } else {
    ledger(`bug7 part2 NOTE: job ${p2.job.status} failed in stage ${failedStage} (not deploy) — agent ${p2.after.status}; recorded`);
  }
  ledger(`bug7 end: part1 ${p1.after.status}/${p1.job.status} marker=${tagVisible}; part2 ${p2.after.status}/${p2.job.status} stage=${failedStage}`);
});

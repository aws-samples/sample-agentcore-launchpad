import { expect, test } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { attach, evidence, EVIDENCE_DIR, ledger, setLanguage } from "./helpers";
import { selectOptions } from "./skilllab-helpers";

/**
 * §4.2 (N13) — Skill Lab is reachable and provisioned: the nav shows "Skill Lab" (en) /
 * "技能实验室" (zh-CN) because `skill_lab` is in `/api/capabilities`; `GET /api/skill-lab/status`
 * is provisioned with the OpenCode-only backend (Kimi K3 target / GLM 5.3 judge); the wizards'
 * execution-backend picker offers exactly one option (`opencode_exec`) — Claude Code / Codex
 * are gone; the Tasksets / Eval / Train tabs render.
 */
test("case 23: Skill Lab nav (en + zh-CN), status JSON, backend picker = opencode_exec only", async ({ page }, testInfo) => {
  const caps = await (await page.request.get("/api/capabilities")).json();
  const status = await (await page.request.get("/api/skill-lab/status")).json();
  await attach(testInfo, "skill-lab-status.json", status);
  fs.mkdirSync(EVIDENCE_DIR, { recursive: true });
  fs.writeFileSync(path.join(EVIDENCE_DIR, "case23-skill-lab-status.json"), JSON.stringify(status, null, 2));
  ledger(`case23 capabilities=${caps.features.length} skill_lab=${caps.features.includes("skill_lab")} status=${JSON.stringify(status)}`);
  expect(caps.features).toContain("skill_lab");
  expect(status.provisioned, `provisioned (missing: ${JSON.stringify(status.missing)})`).toBe(true);
  expect(status.venv_ready).toBe(true);
  expect(status.target_backends).toEqual(["opencode_exec"]);
  expect(status.default_target_model).toBe("global.moonshotai.kimi-k3");
  expect(status.default_judge_model).toBe("global.zai.glm-5.3");
  expect(status.judge_modes).toContain("chat");

  // nav, en then zh-CN
  await page.goto("/v2");
  await setLanguage(page, "en");
  const navEn = page.locator("aside.v2-side a[href='/v2/skill-lab']");
  await expect(navEn).toBeVisible({ timeout: 30_000 });
  const enLabel = (await navEn.innerText()).trim();
  await evidence(page, "case23-nav-en");
  await setLanguage(page, "zh-CN");
  const navZh = page.locator("aside.v2-side a[href='/v2/skill-lab']");
  await expect(navZh).toBeVisible({ timeout: 30_000 });
  const zhLabel = (await navZh.innerText()).trim();
  await evidence(page, "case23-nav-zh-CN");
  await setLanguage(page, "en");
  ledger(`case23 nav labels en="${enLabel}" zh-CN="${zhLabel}"`);
  expect(enLabel).toMatch(/Skill Lab/);
  expect(zhLabel).toMatch(/技能实验室/);

  // page + tabs
  await page.goto("/v2/skill-lab");
  await expect(page.getByTestId("v2-skilllab-unprovisioned")).toHaveCount(0);
  await expect(page.getByTestId("v2-taskset-table")).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case23-skill-lab-tasksets");
  await page.goto("/v2/skill-lab?tab=eval");
  await expect(page.getByTestId("v2-eval-table")).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case23-skill-lab-eval");
  await page.goto("/v2/skill-lab?tab=train");
  await expect(page.getByTestId("v2-train-table")).toBeVisible({ timeout: 60_000 });
  await evidence(page, "case23-skill-lab-train");

  // backend picker in the eval wizard: exactly one option, OpenCode; models prefilled
  await page.goto("/v2/skill-lab?tab=eval&view=new");
  await expect(page.getByTestId("v2-eval-backend")).toBeVisible({ timeout: 60_000 });
  const options = await selectOptions(page, "v2-eval-backend");
  const target = await page.getByTestId("v2-eval-target-model").inputValue();
  const judge = await page.getByTestId("v2-eval-judge-model").inputValue();
  const bodyText = await page.locator("body").innerText();
  await evidence(page, "case23-eval-wizard-backend-picker");
  ledger(`case23 backend options=${JSON.stringify(options)} target=${target} judge=${judge} claude/codex mentioned=${/claude code|codex/i.test(bodyText)}`);
  expect(options.length, "exactly one execution backend").toBe(1);
  expect(options[0]).toMatch(/opencode/i);
  expect(target).toBe("global.moonshotai.kimi-k3");
  expect(judge).toBe("global.zai.glm-5.3");
  expect(/claude code|codex/i.test(bodyText), "no Claude Code / Codex copy").toBe(false);
  ledger("case23 end PASS");
});

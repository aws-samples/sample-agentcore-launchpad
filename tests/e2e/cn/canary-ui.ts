import { expect, type APIRequestContext, type Page } from "@playwright/test";
import { ledger } from "./helpers";
import { selectOptions, selectV2 } from "./skilllab-helpers";

/**
 * Runtime-canary UI drivers shared by the N12 campaign (`48-experiment-canary.spec.ts`) and the
 * storyline (`scenario/s08-improve.spec.ts`).
 *
 * N12-obs49 (user decision 2026-10-16): the V2 Experiments module is canary-only — the
 * configuration-bundle experiment has no console surface any more (and, since N13-obs2, no API either), and its
 * deep links (`?mode=configuration`, `?view=new|detail&id=`) land on the canary list. The specs
 * therefore drive the canary through CanaryStart (agent → candidate prompt → evaluators + primary,
 * N12-obs46 → CREATE → detail) with HARD expectations — a form that does not render or does not
 * create a row fails the step instead of silently falling back to the API, so a UI regression is seen.
 */
export const EXPERIMENTS_URL = "/v2/eval/experiments";
/** `ONLINE_EVAL_DEFAULT` of `frontend/src/lib/experiments.ts` — pre-selected by CanaryStart. */
export const CANARY_DEFAULT_EVALUATORS = ["Builtin.GoalSuccessRate", "Builtin.Helpfulness"] as const;

const escapeRe = (s: string): string => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** N12-obs49 — the configuration deep links land on the canary list (hard). Returns the landing URL. */
export async function expectConfigurationRedirect(page: Page, label: string): Promise<string> {
  for (const deep of ["?mode=configuration", "?view=detail&id=exp_000000000000"]) {
    await page.goto(`${EXPERIMENTS_URL}${deep}`);
    await expect
      .poll(() => { const u = new URL(page.url()); return `${u.pathname}|${u.searchParams.get("mode") ?? ""}|${u.searchParams.has("view")}|${u.searchParams.has("id")}`; }, { timeout: 30_000, message: `${deep} redirects to the canary list` })
      .toBe(`${EXPERIMENTS_URL}||false|false`);
    await expect(page.getByTestId("v2-canary-new").or(page.getByTestId("v2-canary-table")).first(), "canary list rendered").toBeVisible({ timeout: 60_000 });
    ledger(`${label} ${EXPERIMENTS_URL}${deep} → ${page.url()} (canary list, N12-obs49)`);
  }
  return page.url();
}

export interface CanaryUiOptions {
  agentId: string;
  agentName: string;
  candidatePrompt: string;
  /** evaluator id for the PRIMARY select (default: Goal success rate, the UI's own proposal) */
  primary?: string;
  label: string;
  /** evidence hook called with the filled form before CREATE */
  onForm?: (page: Page) => Promise<void>;
}

/** Create a runtime canary through CanaryStart — hard: every control must render, CREATE must land on the detail. */
export async function createCanaryViaUi(page: Page, api: APIRequestContext, o: CanaryUiOptions): Promise<Record<string, unknown>> {
  const known = new Set((((await (await api.get("/api/runtime-canaries", { timeout: 60_000 })).json()) as { canaries: Array<{ id: string }> }).canaries).map((c) => c.id));
  await page.goto(`${EXPERIMENTS_URL}?canary=new`);
  await expect(page.getByTestId("v2-canary-agent"), "CanaryStart renders (champion picker)").toBeVisible({ timeout: 60_000 });
  await selectV2(page, "v2-canary-agent", new RegExp(escapeRe(o.agentName)));
  const promptBox = page.getByTestId("v2-canary-prompt");
  await expect(promptBox, "candidate prompt editor").toBeVisible({ timeout: 30_000 });
  await promptBox.fill(o.candidatePrompt);
  // N12-obs46 — evaluators + primary: the default pair is pre-selected, the primary select lists exactly the selection
  await expect(page.getByTestId("v2-canary-evaluators"), "evaluator picker").toBeVisible({ timeout: 30_000 });
  for (const id of CANARY_DEFAULT_EVALUATORS) {
    await expect(page.getByTestId(`v2-canary-eval-${id}`), `${id} pre-selected`).toBeChecked({ timeout: 30_000 });
  }
  const primaryId = o.primary ?? CANARY_DEFAULT_EVALUATORS[0];
  const primaryOptions = await selectOptions(page, "v2-canary-primary");
  expect(primaryOptions.length, `primary select offers the selected evaluators (${primaryOptions.join(" | ")})`).toBe(CANARY_DEFAULT_EVALUATORS.length);
  const primaryLabel = primaryOptions.find((l) => new RegExp(primaryId === "Builtin.GoalSuccessRate" ? /goal/i : /helpful/i).test(l)) ?? primaryOptions[0];
  await selectV2(page, "v2-canary-primary", primaryLabel);
  ledger(`${o.label} CanaryStart: champion ${o.agentName}, evaluators ${CANARY_DEFAULT_EVALUATORS.join(",")}, primary "${primaryLabel}" (${primaryId})`);
  if (o.onForm) await o.onForm(page);
  const create = page.getByTestId("v2-canary-create");
  await expect(create, "CREATE enabled").toBeEnabled({ timeout: 30_000 });
  await create.click();
  await page.waitForURL((u) => { const c = u.searchParams.get("canary"); return !!c && c !== "new"; }, { timeout: 60_000 });
  const id = new URL(page.url()).searchParams.get("canary")!;
  expect(known.has(id), `canary ${id} is a new row`).toBeFalsy();
  const row = (await (await api.get(`/api/runtime-canaries/${id}`, { timeout: 60_000 })).json()) as Record<string, unknown>;
  expect(String(row.champion_agent_id), "champion is the picked agent").toBe(o.agentId);
  const flat = JSON.stringify(row);
  for (const ev of CANARY_DEFAULT_EVALUATORS) expect(flat, `row carries evaluator ${ev}`).toContain(ev);
  // the row keeps the primary under `artifacts.primary_evaluator` (create) / `evaluators.primary` (view)
  expect(flat, `row carries the primary evaluator ${primaryId}`).toMatch(new RegExp(`"(primary_evaluator|primary)":"${escapeRe(primaryId)}"`));
  await expect(page.getByTestId("v2-canary-detail-evaluators"), "detail shows the evaluator set").toBeVisible({ timeout: 60_000 });
  ledger(`${o.label} canary ${id} created through CanaryStart → ${page.url()}`);
  return row;
}

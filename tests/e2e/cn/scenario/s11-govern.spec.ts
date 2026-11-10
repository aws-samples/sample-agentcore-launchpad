import { expect, test, type APIRequestContext, type Page, type TestInfo } from "@playwright/test";
import { chatOnce, getAgent, ledger, loginFresh, looksLikeError, waitForDeploy } from "../helpers";
import {
  GATEWAY_TOOL,
  isScnPolicy,
  pickOption,
  pollUntil,
  record,
  runStamp,
  SCN_POLICY_PREFIX,
  snap,
  softly,
  stateRequire,
  stateSet,
  stepGate,
  TOKENS,
  TOOL_QUESTION,
} from "./scn";

/**
 * s11 — govern (D20 Policy Phase 1; plan §3 step 11, `docs/superpowers/plans/2026-10-14-d20-policy.md`
 * §6; REQUIREMENTS §4.1 cases 32–37). Runs after s09 and BEFORE s10: the lifecycle step deletes
 * the HR assistant these policies target (`scenario.config.ts` orders the projects; `scn.STEPS`
 * carries the same order for `SCENARIO_RESUME_FROM`).
 *
 *   a posture   `policy` capability, `log_only`, 0 active; Governance → Policies tab; home health row
 *   b author    three drafts from the console editor templates → VALIDATE ok → SIMULATE (drafts):
 *               member invoke ALLOW, member call_tool get_employee DENY, admin call_tool ALLOW
 *   c publish   log_only: ACTIVE set, `services.policy` lit, a member Chat turn is `allow`ed and
 *               audited (hub / chat / invoke), never a 403, no `would_deny` for invoke
 *   d enforce   member PUT → 403; admin without evidence → 422 `policy.confirmation_required`;
 *               typed confirmation → `enforce`; member still allowed → `forbid members invoke <HR>`
 *               published → member: 403 `policy.denied` {policy_ids, decision_id, mode} and the
 *               PolicyDenyCard; admin 200; audit `deny` rows (hub / chat)
 *   e apply     `policy_stale` true → `POST /apply {agent_ids:[HR]}` 202, one job ≤ 10 min →
 *               `policy_stale` false, `policy_coverage` valid; admin tool call still answers
 *   f restore   mode `log_only`, retire + delete the `e2es_` policies → 0 active; audit rows remain
 *
 * Why the forbid names the concrete `hr-database___get_employee` rather than the declared
 * `hr-database___*` pattern the editor chip offers: Phase-1 deploy-time narrowing decides
 * `Agent::<id>` × call_tool, and an agent is not `in Role::"admin"`, so a pattern-level "forbid for
 * members" would remove the whole target from the agent (Gateway dropped for everyone) at s11e. A
 * literal under the kept wildcard is `unenforceable` at list time (coverage `partially_covered`) and
 * the admin probe still reaches the tool — the Phase-1 gap the Phase-2 runtime guard closes (plan
 * §2.3). The console template is still exercised (built, then narrowed in the Cedar textarea).
 *
 * Policy names are `e2es_<thing>_<stamp>` (`POLICY_NAME_PATTERN` forbids dashes); the cleanup spec
 * deletes them and resets the mode when a run dies mid-step.
 */
test.describe.configure({ mode: "serial" });

const HOTLINE_PROMPT = "What is the HR hotline code? Reply with the code and nothing else.";
const NS = "Launchpad";
const COVERAGE_CLASSES = ["full", "partially_covered", "iam_only"];
const SETTINGS_URL = "/api/policies/settings";

interface PolicyRow {
  id: string;
  name: string;
  status: string;
  active_version: number | null;
  draft_version: number | null;
  statement?: string;
  [k: string]: unknown;
}
interface PolicySettings {
  mode: string;
  active_policies: number;
  evidence: { would_deny: number; since: string; window: string };
  stale_agents: string[];
  [k: string]: unknown;
}
interface Decision {
  id: string;
  source: string;
  channel: string;
  principal_kind: string;
  principal_id: string;
  principal_label?: string | null;
  action: string;
  decision: string;
  mode: string;
  agent_id: string | null;
  policy_ids: string[];
  created_at: string;
  [k: string]: unknown;
}
interface SimOut {
  outcome: string;
  determining_policy_ids: string[];
  mode: string;
  [k: string]: unknown;
}
interface Envelope {
  code?: string;
  message?: string;
  detail?: Record<string, unknown> | null;
}
interface Capabilities {
  features: string[];
  policy?: { mode: string; active_policies: number; runtime_guard: boolean };
}
interface Overview {
  services: Record<string, boolean>;
  service_detail?: Record<string, string>;
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

async function settings(api: APIRequestContext): Promise<PolicySettings> {
  const res = await api.get(SETTINGS_URL, { timeout: 60_000 });
  expect(res.ok(), `GET ${SETTINGS_URL} → ${res.status()}`).toBeTruthy();
  return (await res.json()) as PolicySettings;
}

async function policies(api: APIRequestContext): Promise<PolicyRow[]> {
  const res = await api.get("/api/policies", { timeout: 60_000 });
  expect(res.ok(), `GET /api/policies → ${res.status()}`).toBeTruthy();
  return ((await res.json()) as { policies: PolicyRow[] }).policies;
}

async function capabilities(api: APIRequestContext): Promise<Capabilities> {
  const res = await api.get("/api/capabilities", { timeout: 60_000 });
  expect(res.ok()).toBeTruthy();
  return (await res.json()) as Capabilities;
}

async function overview(api: APIRequestContext): Promise<Overview> {
  const res = await api.get("/api/overview", { timeout: 60_000 });
  expect(res.ok(), `GET /api/overview → ${res.status()}`).toBeTruthy();
  return (await res.json()) as Overview;
}

async function simulate(api: APIRequestContext, body: Record<string, unknown>): Promise<SimOut> {
  const res = await api.post("/api/policies/simulate", { data: body, timeout: 60_000 });
  expect(res.status(), `POST /api/policies/simulate ${JSON.stringify(body).slice(0, 160)} → ${res.status()} ${(await res.text()).slice(0, 200)}`).toBe(200);
  return (await res.json()) as SimOut;
}

async function validate(api: APIRequestContext, statement: string): Promise<{ ok: boolean; errors: unknown[] }> {
  const res = await api.post("/api/policies/validate", { data: { statement }, timeout: 60_000 });
  expect(res.status(), "POST /api/policies/validate").toBe(200);
  return (await res.json()) as { ok: boolean; errors: unknown[] };
}

async function publish(api: APIRequestContext, policyId: string, reason: string): Promise<PolicyRow> {
  const res = await api.post(`/api/policies/${policyId}/publish`, { data: { reason }, timeout: 120_000 });
  const text = await res.text();
  ledger(`s11 publish ${policyId} → ${res.status()} ${text.slice(0, 160).replace(/\n/g, " ")}`);
  expect(res.status(), `publish ${policyId}`).toBe(200);
  return JSON.parse(text) as PolicyRow;
}

async function decisions(api: APIRequestContext, query: string): Promise<Decision[]> {
  const res = await api.get(`/api/policies/audit?${query}`, { timeout: 60_000 });
  expect(res.ok(), `GET /api/policies/audit?${query} → ${res.status()}`).toBeTruthy();
  return ((await res.json()) as { decisions: Decision[] }).decisions;
}

/** ISO stamp two minutes in the past — `since` filters tolerate a small client/server clock skew. */
const sinceNow = () => new Date(Date.now() - 120_000).toISOString();

const byMember = (username: string) => (d: Decision) => d.principal_kind === "user" && (d.principal_id === username || d.principal_label === username);

/**
 * Author one draft through the console editor: name → prefill card → (tool chip) → BUILD DRAFT →
 * optional override of the Cedar text → VALIDATE (UI + API, `ok` hard) → CREATE → 201. Returns the
 * ledger id and both the template output and the statement that was saved.
 */
async function authorViaEditor(
  page: Page,
  api: APIRequestContext,
  testInfo: TestInfo,
  o: { name: string; prefill: string; toolChip?: string; statementOverride?: string; label: string },
): Promise<{ id: string; name: string; built: string; statement: string }> {
  await page.goto("/v2/governance?view=policy");
  await expect(page.getByTestId("v2-policy-definition")).toBeVisible({ timeout: 60_000 });
  await page.getByTestId("v2-policy-name").fill(o.name);
  await page.getByTestId(`v2-policy-prefill-${o.prefill}`).click();
  if (o.toolChip) {
    const chip = page.getByTestId(`v2-policy-tool-${o.toolChip}`);
    await expect(chip, `tool chip ${o.toolChip} offered by the entity catalog`).toBeVisible({ timeout: 30_000 });
    await chip.click();
  }
  await page.getByTestId("v2-policy-build-draft").click();
  const cedar = page.getByTestId("v2-policy-cedar");
  await expect(cedar, "BUILD DRAFT fills the Cedar textarea").not.toHaveValue("");
  const built = await cedar.inputValue();
  if (o.statementOverride) await cedar.fill(o.statementOverride);
  const statement = await cedar.inputValue();
  await page.getByTestId("v2-policy-validate").click();
  await expect(page.getByTestId("v2-policy-validation")).toBeVisible({ timeout: 30_000 });
  const report = await validate(api, statement);
  await record(testInfo, `${o.label}-validate.json`, { statement, report }, `validate ok=${report.ok} errors=${JSON.stringify(report.errors).slice(0, 160)}`);
  expect(report.ok, `${o.label}: the statement validates against the ${NS} schema`).toBe(true);
  const uiReport = (await page.getByTestId("v2-policy-validation").innerText().catch(() => "")).trim();
  softly(testInfo, `${o.label}: the editor shows the OK validation report`, () => expect(uiReport).toMatch(/parses and matches/i));
  await snap(page, `s11b-editor-${o.label}`, o.name);
  await page.getByTestId("v2-policy-save").click();
  await expect(page.getByTestId("v2-policy-confirm")).toBeVisible();
  const created = page.waitForResponse((r) => new URL(r.url()).pathname === "/api/policies" && r.request().method() === "POST", { timeout: 60_000 });
  await page.getByTestId("v2-policy-confirm-ok").click();
  const res = await created;
  const body = (await res.json().catch(async () => ({ raw: await res.text() }))) as PolicyRow & { raw?: string };
  ledger(`s11b create ${o.name} → ${res.status()} id=${body.id ?? "?"} status=${body.status ?? "?"} draft=v${body.draft_version ?? "?"}`);
  expect(res.status(), `POST /api/policies (${o.name})`).toBe(201);
  expect(body.status).toBe("draft");
  expect(body.draft_version).toBe(1);
  await expect(page, "the editor re-opens on the created policy").toHaveURL(/view=policy&policy=/, { timeout: 30_000 });
  return { id: body.id, name: body.name, built, statement };
}

/** A Chat turn the hub gate refuses: the thread shows the PolicyDenyCard (no agent bubble, no red error row). */
async function deniedTurn(page: Page, agentId: string, prompt: string): Promise<{ mode: string; reason: string }> {
  await page.goto(`/v2/chat?agent=${encodeURIComponent(agentId)}`);
  await expect(page.getByTestId("chat-input")).toBeVisible();
  await page.getByTestId("chat-input").fill(prompt);
  await page.getByTestId("chat-send").click();
  await expect(page.getByTestId("policy-deny-card"), "PolicyDenyCard rendered for the refused invoke").toBeVisible({ timeout: 120_000 });
  const mode = (await page.getByTestId("policy-deny-mode").innerText().catch(() => "")).trim();
  const reason = (await page.getByTestId("policy-deny-reason").innerText().catch(() => "")).trim();
  return { mode, reason };
}

test("s11a — posture: `policy` capability, log_only, 0 active, no e2es_ leftovers; Governance → Policies tab; home health row", async ({ page }, testInfo) => {
  stepGate("s11");
  test.setTimeout(10 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  stateRequire("member", "s00");
  const api = page.request;

  const caps = await capabilities(api);
  await record(testInfo, "capabilities.json", caps, `features=${caps.features.length} policy=${JSON.stringify(caps.policy)}`);
  expect(caps.features, "the `policy` feature is served (engine registered)").toContain("policy");
  expect(caps.policy?.mode, "workspace mode at the start").toBe("log_only");
  expect(caps.policy?.active_policies, "no ACTIVE policy at the start").toBe(0);

  const s = await settings(api);
  await record(testInfo, "settings-before.json", s, `mode=${s.mode} active=${s.active_policies} would_deny=${s.evidence.would_deny}/${s.evidence.window} stale=${s.stale_agents.length}`);
  expect(s.mode).toBe("log_only");
  expect(s.active_policies).toBe(0);
  const rows = await policies(api);
  const leftovers = rows.filter((p) => isScnPolicy(p.name)).map((p) => `${p.name}:${p.status}`);
  ledger(`s11a leftover ${SCN_POLICY_PREFIX} policies=${JSON.stringify(leftovers)} (all policies: ${rows.map((p) => `${p.name}:${p.status}`).join(",") || "none"})`);
  expect(leftovers, "a fresh environment carries no e2es_ policy (run the cleanup first)").toEqual([]);

  const ov = await overview(api);
  await record(testInfo, "overview.json", { services: ov.services, service_detail: ov.service_detail }, `services.policy=${ov.services.policy} detail=${ov.service_detail?.policy ?? ""}`);
  expect(ov.services.policy, "Overview `services.policy` is off while no policy is ACTIVE (plan §2.6)").toBe(false);
  softly(testInfo, "service_detail.policy reads `log_only · 0 active`", () => expect(String(ov.service_detail?.policy ?? "")).toMatch(/log_only.*0 active/));

  const ws = (await (await api.get("/api/workspaces", { timeout: 60_000 })).json()) as { workspaces: Array<{ id: string; name?: string; is_default: boolean }> };
  const def = ws.workspaces.find((w) => w.is_default);
  expect(def, "default workspace").toBeTruthy();

  await page.goto("/v2/governance?tab=policies");
  await expect(page.getByTestId("v2-tab-policies"), "Governance → Policies tab").toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("v2-policy-settings")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("v2-policy-mode")).toHaveText(/log only/i);
  await expect(page.getByTestId("v2-policy-list")).toBeVisible();
  const hintShown = await page.getByText(/No policy is active yet/i).isVisible().catch(() => false);
  softly(testInfo, "first-policy hint shown while 0 active", () => expect(hintShown).toBe(true));
  await snap(page, "s11a-policies", `mode=${s.mode} active=${s.active_policies}`, { key: true });

  await page.goto("/v2");
  const row = page.getByTestId("v2-health-policy");
  await expect(row, "home health has a `policy` row (feature on)").toBeVisible({ timeout: 60_000 });
  const rowText = (await row.innerText()).replace(/\s+/g, " ").trim();
  const hint = (await row.locator(".state").getAttribute("title")) ?? "";
  ledger(`s11a home health policy row: "${rowText}" title="${hint}"`);
  expect(rowText, "the row is not Ready before the first published policy").not.toMatch(/\bReady\b/);
  softly(testInfo, "home health policy row carries the usage hint", () => expect(rowText).toMatch(/Lights up after the first published policy/i));
  await snap(page, "s11a-home-health", rowText);

  stateSet("govern", { workspace_id: def!.id, workspace_name: String(def!.name ?? def!.id), audit_since: sinceNow() });
  ledger(`s11a PASS mode=${s.mode} active=0 workspace=${def!.id} hr=${hr.name}`);
});

test("s11b — author via the console editor: permit declared tools, forbid get_employee for members, baseline permit invoke → VALIDATE ok → SIMULATE drafts (member invoke ALLOW, member tool DENY, admin tool ALLOW)", async ({ page }, testInfo) => {
  stepGate("s11");
  test.setTimeout(20 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  const member = stateRequire("member", "s00");
  const govern = stateRequire("govern", "s11");
  const api = page.request;
  const stamp = runStamp();

  const entities = (await (await api.get("/api/policies/entities", { timeout: 60_000 })).json()) as {
    tools: Array<{ name: string; pattern: boolean; agent_ids: string[] }>;
    templates: Array<{ name: string; statement: string }>;
    users: Array<{ username: string; role: string }>;
  };
  const hrTools = entities.tools.filter((t) => t.name.startsWith("hr-database___") && t.agent_ids.includes(hr.id));
  await record(testInfo, "entities-tools.json", entities.tools, `hr tools=${hrTools.map((t) => t.name).join(",")}`);
  expect(hrTools.length, "the HR assistant's declared hr-database tools are in the entity catalog").toBeGreaterThan(0);
  const chip = hrTools.find((t) => t.name === "hr-database___*") ?? hrTools[0];
  if (!chip.pattern) ledger(`s11b note: the HR assistant declares the literal ${chip.name}, not the hr-database___* pattern — s11e's narrowing may REMOVE it (see header)`);

  // 1 — permit any principal to call the HR agent's declared target (the agent's own deploy-time permit; admins ALLOW below)
  const permitTools = await authorViaEditor(page, api, testInfo, { name: `${SCN_POLICY_PREFIX}permit_hr_tools_${stamp}`, prefill: "permit_agent_declared_tools", toolChip: chip.name, label: "permit-tools" });
  expect(permitTools.statement).toMatch(/permit\s*\(/);

  // 2 — forbid for members: the template on the chip, narrowed to the concrete tool (header)
  const forbidStatement = `forbid(\n  principal,\n  action == ${NS}::Action::"call_tool",\n  resource == ${NS}::Tool::"${GATEWAY_TOOL}"\n) unless { principal in ${NS}::Role::"admin" };`;
  const forbidTool = await authorViaEditor(page, api, testInfo, { name: `${SCN_POLICY_PREFIX}forbid_hr_tool_${stamp}`, prefill: "forbid_tool_for_members", toolChip: chip.name, statementOverride: forbidStatement, label: "forbid-tool" });
  softly(testInfo, "forbid_tool_for_members template built a forbid on the chip", () => {
    expect(forbidTool.built).toMatch(/forbid\s*\(/);
    expect(forbidTool.built).toContain(chip.name);
    expect(forbidTool.built).toMatch(/Role::"admin"/);
  });

  // 3 — baseline permit invoke: the console prefill, then the server's baseline template (member + admin + hub)
  const serverBaseline = entities.templates.find((t) => t.name === "permit_members_invoke")?.statement ?? "";
  expect(serverBaseline, "server template permit_members_invoke").not.toBe("");
  const permitInvoke = await authorViaEditor(page, api, testInfo, { name: `${SCN_POLICY_PREFIX}permit_invoke_${stamp}`, prefill: "permit_members_invoke", statementOverride: serverBaseline, label: "permit-invoke" });
  const consolePermits = (permitInvoke.built.match(/permit\s*\(/g) ?? []).length;
  const serverPermits = (serverBaseline.match(/permit\s*\(/g) ?? []).length;
  ledger(`s11b console permit_members_invoke prefill: ${consolePermits} permit(s); server baseline: ${serverPermits} (console admin=${/Role::"admin"/.test(permitInvoke.built)} hub=${/System::"hub"/.test(permitInvoke.built)})`);
  if (consolePermits < serverPermits) {
    testInfo.annotations.push({ type: "observation", description: `console permit_members_invoke prefill carries ${consolePermits} permit(s) (members only) while the server baseline carries ${serverPermits} (members, admins, hub) — under enforce the console-built permit alone would default-deny admins and the hub` });
  }

  // simulate the drafts — the engine's outcomes are the contract (hard); the UI run below is the evidence shot
  const me = (await (await api.get("/api/auth/status", { timeout: 60_000 })).json()) as { username?: string; role?: string };
  const adminUser = me.username ?? entities.users.find((u) => u.role === "admin")?.username ?? "admin";
  const sim = (principal: Record<string, unknown>, action: string, resource: Record<string, unknown>, context: Record<string, unknown> = {}) =>
    simulate(api, { principal, action, resource, context, include_drafts: true, record: false });
  const memberInvoke = await sim({ kind: "user", id: member.username, role: "member" }, "invoke", { kind: "agent", id: hr.id });
  const memberTool = await sim({ kind: "user", id: member.username, role: "member" }, "call_tool", { kind: "tool", id: GATEWAY_TOOL }, { agent_id: hr.id });
  const adminTool = await sim({ kind: "user", id: adminUser, role: "admin" }, "call_tool", { kind: "tool", id: GATEWAY_TOOL }, { agent_id: hr.id });
  await record(testInfo, "simulate.json", { memberInvoke, memberTool, adminTool }, `member invoke=${memberInvoke.outcome} member tool=${memberTool.outcome} admin tool=${adminTool.outcome}`);
  expect(memberInvoke.outcome, "member invoke (drafts) → ALLOW").toBe("ALLOW");
  expect(memberTool.outcome, `member call_tool ${GATEWAY_TOOL} (drafts) → DENY`).toBe("DENY");
  expect(memberTool.determining_policy_ids.some((id) => id.startsWith(`${forbidTool.id}:`)), `the forbid ${forbidTool.id}:v1 determines the DENY (got ${memberTool.determining_policy_ids.join(",")})`).toBe(true);
  expect(adminTool.outcome, `admin call_tool ${GATEWAY_TOOL} (drafts) → ALLOW`).toBe("ALLOW");

  await page.goto("/v2/governance?view=simulate");
  await expect(page.getByTestId("v2-policy-simulator")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("v2-policy-sim-principal")).toBeEnabled({ timeout: 60_000 });
  await pickOption(page, "v2-policy-sim-principal", new RegExp(`^${escapeRe(member.username)}\\b`));
  await pickOption(page, "v2-policy-sim-action", /^call_tool\b/);
  await pickOption(page, "v2-policy-sim-resource", new RegExp(`^${escapeRe(chip.name)}`));
  const example = page.getByTestId("v2-policy-sim-example");
  if (await example.isVisible().catch(() => false)) await example.fill(GATEWAY_TOOL);
  await page.getByTestId("v2-policy-sim-context").fill(JSON.stringify({ agent_id: hr.id }));
  const drafts = page.getByTestId("v2-policy-sim-drafts");
  if (!(await drafts.isChecked())) await drafts.check();
  await page.getByTestId("v2-policy-sim-run").click();
  await expect(page.getByTestId("v2-policy-sim-result")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("v2-policy-sim-outcome"), "simulator shows DENY for the member").toHaveText(/deny/i);
  await snap(page, "s11b-simulate", `member call_tool ${GATEWAY_TOOL} → DENY`, { key: true });

  stateSet("govern", { ...govern, tool_pattern: chip.name, tool_is_pattern: chip.pattern, policies: { permit_tools: permitTools.id, forbid_tool: forbidTool.id, permit_invoke: permitInvoke.id } });
  ledger(`s11b PASS drafts: ${permitTools.name}=${permitTools.id} ${forbidTool.name}=${forbidTool.id} ${permitInvoke.name}=${permitInvoke.id}`);
});

test("s11c — publish in log_only (no apply): 3 ACTIVE, services.policy lit; a member Chat turn is allowed and audited `allow` (hub/chat/invoke), no would_deny for invoke", async ({ page, browser }, testInfo) => {
  stepGate("s11");
  test.setTimeout(25 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  const member = stateRequire("member", "s00");
  const govern = stateRequire("govern", "s11");
  const pol = govern.policies;
  if (!pol) throw new Error("scenario state 'govern.policies' missing — run s11b first");
  const api = page.request;
  const baseURL = testInfo.project.use.baseURL as string;

  // the forbid through the editor's PUBLISH dialog (log-only note, apply unchecked), the permits through the API
  await page.goto(`/v2/governance?view=policy&policy=${encodeURIComponent(pol.forbid_tool)}`);
  await expect(page.getByTestId("v2-policy-rollout")).toBeVisible({ timeout: 60_000 });
  await page.getByTestId("v2-policy-publish").click();
  await expect(page.getByTestId("v2-policy-confirm")).toBeVisible();
  await page.getByTestId("v2-policy-publish-reason").fill("e2e scenario s11c — publish in log_only");
  expect(await page.getByTestId("v2-policy-publish-apply").isChecked(), "apply is off by default").toBe(false);
  await snap(page, "s11c-publish-dialog");
  const pubRes = page.waitForResponse((r) => r.url().includes(`/api/policies/${pol.forbid_tool}/publish`) && r.request().method() === "POST", { timeout: 120_000 });
  await page.getByTestId("v2-policy-confirm-ok").click();
  const pr = await pubRes;
  const published = (await pr.json().catch(() => ({}))) as PolicyRow & { apply?: { requested?: boolean; job_ids?: string[] } };
  await record(testInfo, "publish-forbid.json", published, `→ ${pr.status()} status=${published.status} v${published.active_version} apply=${JSON.stringify(published.apply)}`);
  expect(pr.status(), "publish through the editor").toBe(200);
  expect(published.status).toBe("active");
  expect(published.active_version).toBe(1);
  expect(published.apply?.requested ?? false, "no apply requested").toBe(false);
  expect(published.apply?.job_ids ?? [], "no redeploy job started by a plain publish").toEqual([]);
  for (const id of [pol.permit_tools, pol.permit_invoke]) {
    const row = await publish(api, id, "e2e scenario s11c — publish in log_only");
    expect(row.status).toBe("active");
  }

  const s = await settings(api);
  const caps = await capabilities(api);
  const ov = await overview(api);
  await record(testInfo, "settings-after-publish.json", { settings: s, capabilities: caps.policy, services_policy: ov.services.policy, detail: ov.service_detail?.policy }, `mode=${s.mode} active=${s.active_policies} services.policy=${ov.services.policy} stale=${s.stale_agents.length}`);
  expect(s.mode).toBe("log_only");
  expect(s.active_policies, "three ACTIVE e2es_ policies").toBe(3);
  expect(caps.policy?.active_policies).toBe(3);
  expect(ov.services.policy, "Overview lights `services.policy` once a policy is ACTIVE under a mode other than off").toBe(true);
  softly(testInfo, "service_detail.policy reads `log_only · 3 active`", () => expect(String(ov.service_detail?.policy ?? "")).toMatch(/log_only.*3 active/));
  const hrRow = await getAgent(api, hr.id);
  ledger(`s11c HR agent policy_stale=${String(hrRow.policy_stale)} policy_coverage=${String(hrRow.policy_coverage)} (log_only: recorded, s11e asserts)`);

  // a member turn: log_only never blocks — allowed, audited `allow`
  const since = sinceNow();
  const { page: mp, context: mc } = await loginFresh(browser, member.username, member.password, baseURL);
  await mp.goto(`/v2/chat?agent=${encodeURIComponent(hr.id)}`);
  const turn = await chatOnce(mp, hr.id, HOTLINE_PROMPT);
  await record(testInfo, "member-chat-log-only.txt", turn.answer, `${turn.seconds}s errors=${turn.errors} session=${turn.sessionId.slice(0, 8)}`);
  await snap(mp, "s11c-member-chat", `errors=${turn.errors}`);
  await mc.close();
  expect(turn.errors, "log_only: the member's turn is not refused").toBe(0);
  expect(looksLikeError(turn.answer)).toBe(false);
  softly(testInfo, "hotline code (soft — model instruction)", () => expect(turn.answer).toMatch(new RegExp(TOKENS.hotline, "i")));

  const polled = await pollUntil<{ decisions: Decision[] }>(
    api,
    `/api/policies/audit?agent_id=${encodeURIComponent(hr.id)}&since=${encodeURIComponent(since)}&limit=200`,
    (b) => b.decisions.some((d) => d.source === "hub" && d.action === "invoke" && byMember(member.username)(d)),
    { timeoutMs: 90_000, intervalMs: 5_000, label: "s11c audit" },
  );
  const rows = polled.body.decisions;
  const mine = rows.find((d) => d.source === "hub" && d.action === "invoke" && byMember(member.username)(d))!;
  await record(testInfo, "audit-log-only.json", rows, `${rows.length} rows; member invoke decision=${mine.decision} mode=${mine.mode} channel=${mine.channel}`);
  expect(mine.decision, "the member's invoke is `allow` (baseline permit)").toBe("allow");
  expect(mine.mode).toBe("log_only");
  expect(mine.channel).toBe("chat");
  expect(mine.policy_ids, "the evaluated set names the e2es_ permit").toEqual(expect.arrayContaining([pol.permit_invoke]));
  expect(rows.filter((d) => d.action === "invoke" && d.decision === "would_deny"), "no would_deny for invoke in log_only with the permit in place").toEqual([]);
  const s2 = await settings(api);
  softly(testInfo, "evidence counter would_deny is 0", () => expect(s2.evidence.would_deny).toBe(0));

  await page.goto("/v2/governance?tab=decisions");
  await expect(page.getByTestId("v2-policy-decisions")).toBeVisible({ timeout: 60_000 });
  await snap(page, "s11c-decisions", "member invoke allow (log_only)", { key: true });
  stateSet("govern", { ...govern, log_only_session: turn.sessionId });
  ledger(`s11c PASS active=3 member allow decision=${mine.id} evidence.would_deny=${s2.evidence.would_deny}`);
});

test("s11d — enforce: member PUT 403, admin needs the typed confirmation (422 then 200); member still allowed; `forbid members invoke <HR>` → 403 policy.denied + PolicyDenyCard; admin 200; audit deny rows", async ({ page, browser }, testInfo) => {
  stepGate("s11");
  test.setTimeout(30 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  const member = stateRequire("member", "s00");
  const govern = stateRequire("govern", "s11");
  const pol = govern.policies;
  if (!pol) throw new Error("scenario state 'govern.policies' missing — run s11b first");
  const api = page.request;
  const baseURL = testInfo.project.use.baseURL as string;
  const { page: mp, context: mc } = await loginFresh(browser, member.username, member.password, baseURL);

  // admin-only + typed confirmation (store rule: `confirmation_name` == workspace id when no would_deny evidence)
  const memberPut = await mp.request.put(SETTINGS_URL, { data: { mode: "enforce", confirmation_name: govern.workspace_id, override_reason: "e2e s11d (member)" }, timeout: 60_000 });
  const memberBody = (await memberPut.json().catch(() => ({}))) as Envelope;
  ledger(`s11d member PUT enforce → ${memberPut.status()} ${memberBody.code ?? ""}`);
  expect(memberPut.status(), "a member cannot switch the workspace to enforce").toBe(403);
  const before = await settings(api);
  expect(before.mode).toBe("log_only");
  if (before.evidence.would_deny === 0) {
    const bare = await api.put(SETTINGS_URL, { data: { mode: "enforce" }, timeout: 60_000 });
    const bareBody = (await bare.json().catch(() => ({}))) as Envelope;
    await record(testInfo, "enforce-without-confirmation.json", { status: bare.status(), body: bareBody }, `→ ${bare.status()} ${bareBody.code ?? ""} expected_confirmation=${String(bareBody.detail?.expected_confirmation ?? "")}`);
    expect(bare.status(), "no would_deny evidence → the typed confirmation is required").toBe(422);
    expect(bareBody.code).toBe("policy.confirmation_required");
    expect(String(bareBody.detail?.expected_confirmation ?? ""), "the API expects the workspace id").toBe(govern.workspace_id);
  } else {
    ledger(`s11d evidence.would_deny=${before.evidence.would_deny} in the window — the confirmation-required path is not exercised`);
  }

  // the console dialog (types the workspace NAME the dialog asks for); the API (workspace ID) is the fallback
  await page.goto("/v2/governance?tab=policies");
  await expect(page.getByTestId("v2-policy-settings")).toBeVisible({ timeout: 60_000 });
  await page.getByTestId("v2-policy-settings").getByRole("radio", { name: /^Enforce$/i }).click();
  await expect(page.getByTestId("v2-policy-enforce-dialog")).toBeVisible({ timeout: 30_000 });
  const nameInput = page.getByTestId("v2-policy-enforce-name");
  if (await nameInput.isVisible().catch(() => false)) {
    await nameInput.fill(govern.workspace_name);
    await page.getByTestId("v2-policy-enforce-reason").fill("e2e scenario s11d — typed confirmation");
  }
  await snap(page, "s11d-enforce-dialog", undefined, { key: true });
  let switched = false;
  const okBtn = page.getByTestId("v2-policy-enforce-ok");
  if (await okBtn.isEnabled().catch(() => false)) {
    const putRes = page.waitForResponse((r) => new URL(r.url()).pathname === SETTINGS_URL && r.request().method() === "PUT", { timeout: 60_000 });
    await okBtn.click();
    const pr = await putRes;
    const body = (await pr.json().catch(() => ({}))) as Envelope & { mode?: string };
    ledger(`s11d UI enforce dialog PUT → ${pr.status()} ${body.code ?? body.mode ?? ""}`);
    if (pr.status() === 200) switched = true;
    else testInfo.annotations.push({ type: "observation", description: `enforce dialog: the typed workspace name "${govern.workspace_name}" passed the console blockers but PUT /api/policies/settings answered ${pr.status()} ${body.code ?? ""} (the API compares the workspace id "${govern.workspace_id}")` });
  } else {
    const blockers = (await page.getByTestId("enforce-blockers").innerText().catch(() => "")).trim();
    testInfo.annotations.push({ type: "observation", description: `enforce dialog OK button stayed disabled (${blockers || "no blockers text"})` });
    ledger(`s11d UI enforce dialog blocked: ${blockers}`);
  }
  if (!switched) {
    await page.keyboard.press("Escape").catch(() => {});
    const r = await api.put(SETTINGS_URL, { data: { mode: "enforce", confirmation_name: govern.workspace_id, override_reason: "e2e scenario s11d — typed confirmation (workspace id)" }, timeout: 60_000 });
    const body = (await r.json().catch(async () => ({ raw: await r.text() }))) as Envelope & { mode?: string };
    ledger(`s11d API enforce → ${r.status()} ${body.code ?? body.mode ?? ""}`);
    expect(r.status(), "admin switches to enforce with the typed workspace id").toBe(200);
  }
  const enforced = await settings(api);
  expect(enforced.mode, "workspace mode is enforce").toBe("enforce");
  await page.goto("/v2/governance?tab=policies");
  await expect(page.getByTestId("v2-policy-mode")).toHaveText(/enforce/i, { timeout: 60_000 });
  await snap(page, "s11d-enforce-on", `mode=${enforced.mode}`);

  // the baseline permit still lets the member in
  await mp.goto(`/v2/chat?agent=${encodeURIComponent(hr.id)}`);
  const allowed = await chatOnce(mp, hr.id, HOTLINE_PROMPT);
  await record(testInfo, "member-chat-enforce-allowed.txt", allowed.answer, `${allowed.seconds}s errors=${allowed.errors}`);
  expect(allowed.errors, "enforce + baseline permit: the member's turn is allowed").toBe(0);
  expect(looksLikeError(allowed.answer)).toBe(false);

  // forbid members from invoking the HR assistant (API: validate → create → publish)
  const since = sinceNow();
  const forbidInvoke = `forbid(\n  principal in ${NS}::Role::"member",\n  action == ${NS}::Action::"invoke",\n  resource == ${NS}::Agent::"${hr.id}"\n);`;
  const v = await validate(api, forbidInvoke);
  expect(v.ok, "forbid-invoke statement validates").toBe(true);
  const created = await api.post("/api/policies", { data: { name: `${SCN_POLICY_PREFIX}forbid_member_invoke_${runStamp()}`, statement: forbidInvoke, description: "e2e scenario s11d" }, timeout: 60_000 });
  const forbidRow = (await created.json()) as PolicyRow;
  expect(created.status(), "create forbid-invoke policy").toBe(201);
  const activeForbid = await publish(api, forbidRow.id, "e2e scenario s11d — enforce");
  expect(activeForbid.status).toBe("active");
  stateSet("govern", { ...govern, policies: { ...pol, forbid_invoke: forbidRow.id }, enforce_at: new Date().toISOString() });

  // member → 403 policy.denied: the envelope (console invoke route, same gate as Chat) …
  const denied = await mp.request.post(`/api/agents/${hr.id}/invoke`, { data: { prompt: HOTLINE_PROMPT }, timeout: 120_000 });
  const deniedBody = (await denied.json().catch(async () => ({ raw: await denied.text() }))) as Envelope;
  await record(testInfo, "member-invoke-denied.json", { status: denied.status(), body: deniedBody }, `→ ${denied.status()} ${deniedBody.code ?? ""} detail=${JSON.stringify(deniedBody.detail).slice(0, 200)}`);
  expect(denied.status(), "member invoke in enforce → 403").toBe(403);
  expect(deniedBody.code).toBe("policy.denied");
  const detail = deniedBody.detail ?? {};
  expect(detail.policy_ids, "detail.policy_ids names the forbid").toEqual(expect.arrayContaining([forbidRow.id]));
  expect(String(detail.decision_id ?? ""), "detail.decision_id present").not.toBe("");
  expect(detail.mode).toBe("enforce");
  // … and the Chat console shows the PolicyDenyCard (no bubble, no red error row)
  const card = await deniedTurn(mp, hr.id, HOTLINE_PROMPT);
  ledger(`s11d PolicyDenyCard mode="${card.mode}" reason="${card.reason.slice(0, 120)}"`);
  await snap(mp, "s11d-deny-card", card.reason.slice(0, 80), { key: true });
  expect(card.mode, "the card shows the enforce mode").toMatch(/enforce/i);
  expect(await mp.locator(".v2-chat-error").count(), "a policy deny is a card, not a red error row").toBe(0);
  await mc.close();

  // admin → 200 (baseline permits Role::admin)
  await page.goto(`/v2/chat?agent=${encodeURIComponent(hr.id)}`);
  const admin = await chatOnce(page, hr.id, HOTLINE_PROMPT);
  await record(testInfo, "admin-chat-enforce.txt", admin.answer, `${admin.seconds}s errors=${admin.errors}`);
  expect(admin.errors, "admin invoke is allowed in enforce").toBe(0);
  expect(looksLikeError(admin.answer)).toBe(false);
  expect(await page.getByTestId("policy-deny-card").count(), "no deny card on the admin's turn").toBe(0);

  const polled = await pollUntil<{ decisions: Decision[] }>(
    api,
    `/api/policies/audit?agent_id=${encodeURIComponent(hr.id)}&decision=deny&since=${encodeURIComponent(since)}&limit=200`,
    (b) => b.decisions.filter((d) => d.source === "hub" && byMember(member.username)(d)).length >= 2,
    { timeoutMs: 90_000, intervalMs: 5_000, label: "s11d audit deny" },
  );
  const denies = polled.body.decisions.filter((d) => d.source === "hub" && byMember(member.username)(d));
  await record(testInfo, "audit-deny.json", polled.body.decisions, `${denies.length} member deny rows (API invoke + Chat)`);
  expect(denies.length, "≥ 2 deny rows for the member (the API invoke and the Chat turn)").toBeGreaterThanOrEqual(2);
  for (const d of denies) {
    expect(d.channel, "deny rows come from the chat channel").toBe("chat");
    expect(d.action).toBe("invoke");
    expect(d.mode).toBe("enforce");
    expect(d.policy_ids).toEqual(expect.arrayContaining([forbidRow.id]));
  }
  expect(denies.some((d) => d.id === String(detail.decision_id)), "the 403's decision_id is one of the audited rows").toBe(true);
  await page.goto("/v2/governance?tab=decisions");
  await expect(page.getByTestId("v2-policy-decisions")).toBeVisible({ timeout: 60_000 });
  await snap(page, "s11d-decisions", `${denies.length} deny rows`, { key: true });
  ledger(`s11d PASS enforce; forbid_invoke=${forbidRow.id}; member 403 decision=${String(detail.decision_id)}; admin ok`);
});

test("s11e — apply the narrowing in enforce: HR assistant stale → POST /apply 202 (one job) → redeploy ≤ 10 min → policy_stale false, coverage valid; admin tool call still answers", async ({ page }, testInfo) => {
  stepGate("s11");
  test.setTimeout(30 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  const govern = stateRequire("govern", "s11");
  const api = page.request;

  const before = await getAgent(api, hr.id);
  const s = await settings(api);
  await record(testInfo, "before-apply.json", { policy_stale: before.policy_stale, policy_coverage: before.policy_coverage, version: before.version, stale_agents: s.stale_agents }, `stale=${String(before.policy_stale)} coverage=${String(before.policy_coverage)} v${before.version}`);
  expect(s.mode).toBe("enforce");
  expect(before.policy_stale, "the HR assistant is stale: the ACTIVE set changed after its last deploy").toBe(true);
  expect(s.stale_agents, "settings.stale_agents lists it").toContain(hr.id);
  await page.goto("/v2/governance?tab=policies");
  await expect(page.getByTestId("v2-policy-settings")).toBeVisible({ timeout: 60_000 });
  const staleShown = await page.getByTestId("v2-policy-stale-agents").isVisible().catch(() => false);
  softly(testInfo, "the stale alert names the HR assistant", () => expect(staleShown).toBe(true));
  await snap(page, "s11e-stale", `${s.stale_agents.length} stale`);

  const v0 = Number(before.version);
  const t0 = Date.now();
  const res = await api.post("/api/policies/apply", { data: { agent_ids: [hr.id] }, timeout: 120_000 });
  const body = (await res.json().catch(async () => ({ raw: await res.text() }))) as { agents?: Array<{ agent_id: string; job_id: string | null; patterns: string[]; gateway_dropped: boolean; coverage: string; applied: boolean; skipped?: string }> };
  await record(testInfo, "apply.json", body, `→ ${res.status()} ${JSON.stringify(body.agents).slice(0, 300)}`);
  expect(res.status(), "POST /api/policies/apply → 202").toBe(202);
  expect(body.agents?.length, "exactly one agent entry").toBe(1);
  const entry = body.agents![0];
  expect(entry.agent_id).toBe(hr.id);
  expect(entry.skipped, `apply not skipped (${entry.skipped ?? ""})`).toBeUndefined();
  expect(entry.job_id, "one redeploy job started").toBeTruthy();
  expect(entry.applied, "enforce: the narrowing is applied, not a dry run").toBe(true);
  // rev-r8 nit B: only when the spec declared the `hr-database___*` pattern does the literal forbid stay unenforceable
  // (target kept); a spec that declares the literal itself loses the tool for everyone at apply — then the admin tool
  // probe below is meaningless and is skipped (recorded, not graded).
  const patternDeclared = govern.tool_is_pattern !== false;
  if (patternDeclared) {
    softly(testInfo, "the declared target is kept (a literal under the wildcard is unenforceable, not removed)", () => {
      expect(entry.gateway_dropped).toBe(false);
      if (govern.tool_pattern) expect(entry.patterns).toContain(govern.tool_pattern);
    });
    softly(testInfo, "coverage partially_covered (literal forbid under a kept wildcard)", () => expect(entry.coverage).toBe("partially_covered"));
  } else {
    ledger(`s11e note: the HR spec declares the literal ${govern.tool_pattern ?? GATEWAY_TOOL} — the forbid removes it at apply (removed/gateway_dropped=${entry.gateway_dropped}); tool-kept checks and the admin tool probe are skipped`);
    testInfo.annotations.push({ type: "observation", description: `s11e: literal declared tool ${govern.tool_pattern ?? GATEWAY_TOOL} removed by the apply — admin tool probe skipped` });
  }

  const dep = await waitForDeploy(page, testInfo, hr.id, "s11e", t0);
  expect(dep.job.status, `apply redeploy failed: ${String(dep.job.error ?? "")}`).toBe("succeeded");
  expect(dep.seconds, "apply redeploy ≤ 10 min").toBeLessThanOrEqual(600);
  const after = await getAgent(api, hr.id);
  const s2 = await settings(api);
  await record(testInfo, "after-apply.json", { policy_stale: after.policy_stale, policy_coverage: after.policy_coverage, version: after.version, status: after.status, stale_agents: s2.stale_agents }, `stale=${String(after.policy_stale)} coverage=${String(after.policy_coverage)} v${after.version} ${after.status}`);
  expect(after.status, "agent active after the apply redeploy").toBe("active");
  expect(COVERAGE_CLASSES, `policy_coverage valid (got ${String(after.policy_coverage)})`).toContain(String(after.policy_coverage));
  expect(after.policy_stale, "policy_stale cleared by the apply").toBe(false);
  expect(s2.stale_agents, "settings.stale_agents no longer lists it").not.toContain(hr.id);
  softly(testInfo, "redeploy minted version +1", () => expect(Number(after.version)).toBe(v0 + 1));
  if (patternDeclared) softly(testInfo, "coverage partially_covered on the agent row", () => expect(String(after.policy_coverage)).toBe("partially_covered"));
  await page.goto(`/v2/agents?view=detail&id=${encodeURIComponent(hr.id)}`);
  await expect(page.getByTestId("v2-agent-basic")).toBeVisible({ timeout: 60_000 });
  await snap(page, "s11e-agent-after-apply", `coverage=${String(after.policy_coverage)} v${after.version}`, { key: true });

  // the admin still reaches the Gateway tool through the narrowed runtime (tool choice is model behaviour → soft);
  // skipped when the spec declared the literal tool (nit B above) — the apply removed it for everyone
  if (patternDeclared) {
    await page.goto(`/v2/chat?agent=${encodeURIComponent(hr.id)}`);
    const tool = await chatOnce(page, hr.id, TOOL_QUESTION);
    await record(testInfo, "admin-tool-after-apply.txt", `${tool.answer}\n\ntools: ${tool.toolNames.join(", ")}`, `${tool.seconds}s errors=${tool.errors} tools=${tool.toolNames.join(",")}`);
    expect(tool.errors, "admin turn after the apply: no error bubble").toBe(0);
    expect(looksLikeError(tool.answer)).toBe(false);
    softly(testInfo, "an hr-database gateway tool was called (soft — model behaviour)", () => expect(tool.toolNames.join(" ")).toMatch(/hr-database___/));
    softly(testInfo, "answer names Maya Chen (soft — model behaviour)", () => expect(tool.answer).toMatch(/Maya Chen/));
    await snap(page, "s11e-admin-tool", `tools=${tool.toolNames.join(",")}`);
  }

  stateSet("govern", { ...govern, apply_job: String(entry.job_id), coverage: String(after.policy_coverage), hr_version_after_apply: Number(after.version) });
  stateSet("hr", { ...hr, version: Number(after.version) });
  ledger(`s11e PASS apply job=${String(entry.job_id)} ${dep.seconds}s coverage=${String(after.policy_coverage)} v${v0} → v${after.version}`);
});

test("s11f — restore: mode log_only, retire + delete the e2es_ policies → 0 active, services.policy off; audit rows remain", async ({ page }, testInfo) => {
  stepGate("s11");
  test.setTimeout(15 * 60 * 1000);
  const hr = stateRequire("hr", "s04");
  const govern = stateRequire("govern", "s11");
  const api = page.request;

  const back = await api.put(SETTINGS_URL, { data: { mode: "log_only" }, timeout: 60_000 });
  expect(back.status(), "mode back to log_only").toBe(200);
  const mine = (await policies(api)).filter((p) => isScnPolicy(p.name));
  expect(mine.length, "the four e2es_ policies are listed").toBeGreaterThanOrEqual(4);
  const retired: string[] = [];
  for (const p of mine.filter((p) => p.status === "active")) {
    const r = await api.post(`/api/policies/${p.id}/retire`, { data: { reason: "e2e scenario s11f — restore" }, timeout: 60_000 });
    const row = (await r.json().catch(() => ({}))) as PolicyRow;
    ledger(`s11f retire ${p.name} → ${r.status()} ${row.status ?? ""}`);
    expect(r.status(), `retire ${p.name}`).toBe(200);
    expect(row.status).toBe("retired");
    retired.push(p.id);
  }
  const deleted: string[] = [];
  for (const p of mine) {
    const r = await api.delete(`/api/policies/${p.id}`, { timeout: 60_000 });
    ledger(`s11f delete ${p.name} → ${r.status()} ${(await r.text()).slice(0, 100)}`);
    expect(r.status(), `delete ${p.name}`).toBe(200);
    deleted.push(p.id);
  }

  const s = await settings(api);
  const caps = await capabilities(api);
  const ov = await overview(api);
  const left = (await policies(api)).filter((p) => isScnPolicy(p.name)).map((p) => p.name);
  await record(testInfo, "restored.json", { settings: s, capabilities: caps.policy, services_policy: ov.services.policy, left, retired, deleted }, `mode=${s.mode} active=${s.active_policies} left=${JSON.stringify(left)}`);
  expect(s.mode).toBe("log_only");
  expect(s.active_policies, "0 ACTIVE after the restore").toBe(0);
  expect(caps.policy?.active_policies).toBe(0);
  expect(ov.services.policy, "services.policy off again").toBe(false);
  expect(left, "no e2es_ policy left").toEqual([]);

  const rows = await decisions(api, `agent_id=${encodeURIComponent(hr.id)}&since=${encodeURIComponent(govern.audit_since)}&limit=500`);
  const hub = rows.filter((d) => d.source === "hub");
  ledger(`s11f audit rows since ${govern.audit_since}: ${rows.length} (hub ${hub.length}: ${hub.map((d) => d.decision).join(",")})`);
  softly(testInfo, "audit rows remain after the policies are deleted (≥ 4 hub decisions)", () => expect(hub.length).toBeGreaterThanOrEqual(4));
  expect(rows.some((d) => d.decision === "deny"), "the enforce-mode deny rows are kept").toBe(true);

  await page.goto("/v2/governance?tab=policies");
  await expect(page.getByTestId("v2-policy-mode")).toHaveText(/log only/i, { timeout: 60_000 });
  await snap(page, "s11f-restored", `mode=${s.mode} active=0`, { key: true });
  const hrRow = await getAgent(api, hr.id);
  ledger(`s11f HR agent policy_stale=${String(hrRow.policy_stale)} after the restore (mode changed; s10's redeploy recomputes the narrowing in log_only — nothing was removed at s11e)`);
  stateSet("govern", { ...govern, restored: true });
  ledger(`s11f PASS retired=${retired.length} deleted=${deleted.length} audit rows=${rows.length}`);
});

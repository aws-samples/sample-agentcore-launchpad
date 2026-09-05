// SE-013 UI probe: RATE LIMITS panel against a stubbed /api (no backend, no AWS).
// Usage: node ui_probe.mjs http://127.0.0.1:5199
import { chromium } from "/home/ubuntu/.nvm/versions/node/v22.19.0/lib/node_modules/playwright/index.mjs";

const BASE = process.argv[2] ?? "http://127.0.0.1:5199";
const OUT = "/home/ubuntu/workspace/agentcore_launchpad/.claude/self-evolution/runs/SE-013";
const results = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"} ${name}${detail ? ` — ${detail}` : ""}`);
};

const gateway = {
  id: "gw-1", arn: "arn:aws:bedrock-agentcore:us-west-2:123:gateway/gw-1", name: "launchpad-gw",
  description: "", status: "READY", status_reasons: [], protocol_type: "MCP",
  authorizer_type: "AWS_IAM", url: "https://gw-1.example.test/mcp", role_arn: "arn:aws:iam::123:role/gw",
  managed: true, target_count: 0, targets: [], policy_engine: null,
  shared_gateways: [{ id: "gw-1", arn: "arn", name: "launchpad-gw" }], shared_engine: false,
  attachability: { attachable: true, reason: null, auth_type: "aws_iam" }, policy_test_available: false,
  registry_record: null, legacy_record_count: 0, updated_at: "2026-09-05T00:00:00+00:00",
  authorizer_configuration: null, protocol_configuration: null, actions: [], iam_preflight: null,
  external_tools_list_command: null,
};
const limit = (id, extra = {}) => ({
  id, gateway_id: "gw-1", description: "per target", dimension_keys: ["targetName", "$.context.jwt.sub"],
  entries: [
    { dimensions: { targetName: "office-facts", "$.context.jwt.sub": "*" },
      requests: [{ rate: 10, period: "second" }], tokens: [{ rate: 5000, period: "minute" }] },
    { dimensions: { targetName: "*", "$.context.jwt.sub": "*" }, requests: [{ rate: 60, period: "minute" }] },
  ],
  status: "ACTIVE", created_at: "2026-09-05T00:00:00+00:00", updated_at: "2026-09-05T01:02:03+00:00", ...extra,
});

const state = { rateLimits: { status: 200, body: { rate_limits: [limit("rl-1"), limit("rl-2", { status: "UPDATING" })] } }, posts: [] };
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
await page.addInitScript(() => localStorage.setItem("i18nextLng", "en"));
const unhandled = new Set();
await page.route("**/api/**", async (route) => {
  const req = route.request();
  const url = new URL(req.url());
  const p = url.pathname;
  const json = (status, body) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
  if (p === "/api/auth/status") return json(200, { auth_required: false, authenticated: true, registration_enabled: false, registration_requires_approval: false, username: null, role: "admin", email: null, account_expires_at: null, permissions: [] });
  if (p === "/api/governance/gateways/gw-1") return json(200, gateway);
  if (p === "/api/governance/gateways/gw-1/policies") return json(200, { policies: [] });
  if (p === "/api/governance/gateways/gw-1/registry-preview") return json(503, { code: "registry.unavailable", message: "stub", detail: null });
  if (p === "/api/governance/gateways/gw-1/decisions") return json(200, { available: true, log_only_count: 0, decisions: [] });
  if (p === "/api/governance/gateways/gw-1/rate-limits") {
    if (req.method() === "POST") {
      state.posts.push(JSON.parse(req.postData()));
      return json(201, limit("rl-new", { status: "CREATING" }));
    }
    return json(state.rateLimits.status, state.rateLimits.body);
  }
  if (p.startsWith("/api/governance/gateways/gw-1/rate-limits/")) {
    state.posts.push({ method: req.method(), path: p, body: req.postData() ? JSON.parse(req.postData()) : null });
    return json(200, req.method() === "DELETE" ? { deleted: true, id: "rl-1", status: "DELETING" } : limit("rl-1", { status: "UPDATING" }));
  }
  unhandled.add(`${req.method()} ${p}`);
  return json(404, { code: "http.404", message: "stub", detail: null });
});

const go = () => page.goto(`${BASE}/governance?view=gateway&gateway=gw-1`, { waitUntil: "networkidle" });
const panel = () => page.locator(".panel", { has: page.locator("h2", { hasText: "RATE LIMITS" }) });

// ── 1. list ──────────────────────────────────────────────────────────────────
await go();
await panel().waitFor();
const rows = await panel().locator('tr[data-testid^="rate-limit-"]').count();
check("list renders two rate limits", rows === 2, `rows=${rows}`);
check("status chips", (await panel().innerText()).includes("ACTIVE") && (await panel().innerText()).includes("UPDATING"));
check("semantics note visible", (await panel().innerText()).includes("evaluated before Policy"));
await panel().locator('tr[data-testid="rate-limit-rl-1"] .gov-rl-toggle').click();
const entriesText = await panel().locator(".gov-rl-entries").innerText();
check("expanded entries show dimension values + metric rates", entriesText.includes("office-facts") && entriesText.includes("requests 10/second") && entriesText.includes("tokens 5000/minute"), entriesText.replace(/\s+/g, " ").slice(0, 160));
const editDisabledReason = await panel().locator('tr[data-testid="rate-limit-rl-2"] button:has-text("EDIT ENTRIES")').getAttribute("title");
check("EDIT on UPDATING row disabled with reason", (editDisabledReason ?? "").includes("UPDATING"), editDisabledReason ?? "");
await page.screenshot({ path: `${OUT}/ui-list.png`, fullPage: true });

// ── 2. empty ─────────────────────────────────────────────────────────────────
state.rateLimits = { status: 200, body: { rate_limits: [] } };
await go();
await panel().waitFor();
check("empty state", (await panel().innerText()).includes("NO RATE LIMITS ON THIS GATEWAY"));

// ── 3. error ─────────────────────────────────────────────────────────────────
state.rateLimits = { status: 403, body: { code: "aws.access_denied", message: "denied", detail: null } };
await go();
await panel().waitFor();
await page.waitForTimeout(300);
const errText = await panel().innerText();
check("error state shows the envelope, not an empty table", errText.includes("aws.access_denied") && !errText.includes("NO RATE LIMITS ON THIS GATEWAY"), errText.replace(/\s+/g, " ").slice(0, 200));
await page.screenshot({ path: `${OUT}/ui-error.png`, fullPage: true });

// ── 4. ADD form: client-side validation ──────────────────────────────────────
state.rateLimits = { status: 200, body: { rate_limits: [] } };
await go();
await panel().waitFor();
await panel().locator('button:has-text("ADD RATE LIMIT")').click();
const form = page.locator('[data-testid="rate-limit-form"]');
await form.waitFor();
const save = form.locator('button:has-text("SAVE")');
check("SAVE disabled on empty form with reason", (await save.isDisabled()) && ((await save.getAttribute("title")) ?? "").includes("dimension key"), (await save.getAttribute("title")) ?? "");
await form.locator('.gov-rl-key-picker button:has-text("targetName")').click();
await form.locator('.gov-rl-key-picker button:has-text("toolName")').click();
await form.locator('button:has-text("ADD ENTRY")').click();
const entry = form.locator('[data-testid="rate-limit-entry-0"]');
const dims = entry.locator(".gov-rl-dimension input");
await dims.nth(0).fill("*");
await dims.nth(1).fill("search");
await entry.locator('.gov-rl-metric input[type="checkbox"]').nth(0).check();
await entry.locator('input[aria-label="requests RATE"]').fill("5");
let blockers = await form.locator('[data-testid="rate-limit-blockers"]').innerText();
check("leading * is blocked client-side", (await save.isDisabled()) && blockers.includes("trailing"), blockers);
check("SAVE explains why (disabledReason)", ((await save.getAttribute("title")) ?? "").includes("trailing"));
await dims.nth(0).fill("office-facts");
await entry.locator('.gov-rl-metric input[type="checkbox"]').nth(1).check(); // tokens
await entry.locator('input[aria-label="tokens RATE"]').fill("1000");
await entry.locator('select[aria-label="tokens PERIOD"]').selectOption("second");
blockers = await form.locator('[data-testid="rate-limit-blockers"]').innerText();
check("tokens per second is blocked client-side", (await save.isDisabled()) && blockers.includes("tokens are per minute only"), blockers);
await entry.locator('select[aria-label="tokens PERIOD"]').selectOption("minute");
await page.waitForTimeout(100);
check("valid draft enables SAVE", !(await save.isDisabled()) && (await form.locator('[data-testid="rate-limit-blockers"]').count()) === 0);
// jwt claim key with invalid then valid claim
await form.locator('input[aria-label="JWT CLAIM"]').fill("a");
check("1-char JWT claim cannot be added", await form.locator('button:has-text("ADD KEY")').isDisabled());
await form.locator('input[aria-label="JWT CLAIM"]').fill("sub");
await form.locator('button:has-text("ADD KEY")').click();
check("jwt key added and entry gained a * for it", (await form.locator('[data-testid="rate-limit-keys"]').innerText()).includes("$.context.jwt.sub") && (await dims.count()) === 3 && (await dims.nth(2).inputValue()) === "*");
await form.locator('input.input:not(.mono)').last().fill("probe description");
await page.screenshot({ path: `${OUT}/ui-form.png`, fullPage: true });
await save.click();
await page.waitForTimeout(500);
const post = state.posts.find((p) => p.dimension_keys);
check("POST body is the wire shape", !!post && JSON.stringify(post) === JSON.stringify({
  dimension_keys: ["targetName", "toolName", "$.context.jwt.sub"],
  entries: [{ dimensions: { targetName: "office-facts", toolName: "search", "$.context.jwt.sub": "*" }, requests: [{ rate: 5, period: "second" }], tokens: [{ rate: 1000, period: "minute" }] }],
  description: "probe description",
}), JSON.stringify(post));
check("form closes after save", (await page.locator('[data-testid="rate-limit-form"]').count()) === 0);

// ── 5. edit keeps keys locked; delete confirms ───────────────────────────────
state.rateLimits = { status: 200, body: { rate_limits: [limit("rl-1")] } };
await go();
await panel().waitFor();
await panel().locator('tr[data-testid="rate-limit-rl-1"] button:has-text("EDIT ENTRIES")').click();
await form.waitFor();
check("edit locks keys (no picker, lock hint)", (await form.locator(".gov-rl-key-picker").count()) === 0 && (await form.innerText()).includes("immutable"));
check("edit hydrates two entries", (await form.locator('[data-testid^="rate-limit-entry-"]').count()) === 2);
await save.click();
await page.waitForTimeout(400);
const put = state.posts.find((p) => p.method === "PUT");
check("PUT sends entries + description only", !!put && Object.keys(put.body).sort().join(",") === "description,entries", JSON.stringify(put?.body).slice(0, 200));
await panel().locator('tr[data-testid="rate-limit-rl-1"] button:has-text("DELETE")').click();
await page.locator('[role="alertdialog"]').waitFor();
check("delete confirm names the rate limit", (await page.locator('[role="alertdialog"]').innerText()).includes("rl-1"));
await page.locator('[role="alertdialog"] button:has-text("DELETE")').click();
await page.waitForTimeout(400);
check("DELETE request sent", state.posts.some((p) => p.method === "DELETE" && p.path.endsWith("/rl-1")));

// ── 6. unmanaged gateway: ADD disabled with reason ───────────────────────────
gateway.managed = false;
await go();
await panel().waitFor();
const addTitle = await panel().locator('button:has-text("ADD RATE LIMIT")').getAttribute("title");
check("unmanaged → ADD disabled with 'manage this Gateway first'", (addTitle ?? "").includes("manage this Gateway first"), addTitle ?? "");

// ── 7. zh-CN renders ─────────────────────────────────────────────────────────
await page.evaluate(() => localStorage.setItem("i18nextLng", "zh-CN"));
await page.addInitScript(() => localStorage.setItem("i18nextLng", "zh-CN"));
await go();
await page.waitForTimeout(300);
check("zh-CN panel title", (await page.locator(".panel h2", { hasText: "限流" }).count()) === 1);

console.log("unhandled api:", [...unhandled].join(", ") || "none");
await browser.close();
const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length ? 1 : 0);

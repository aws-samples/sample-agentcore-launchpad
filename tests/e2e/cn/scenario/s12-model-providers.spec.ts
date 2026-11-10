import { expect, test, type APIRequestContext } from "@playwright/test";
import { ledger, loginFresh } from "../helpers";
import { isScn, record, runStamp, SCN_PREFIX, snap, softly, stateRequire, stateSet, stepGate } from "./scn";

/**
 * s12 — model providers (D21 unified model registry; plan `docs/superpowers/plans/2026-10-14-d21-model-providers.md`
 * §10 e2e row + §11.1 M1-e; contract `docs/cn/interfaces/model-registry.md`; REQUIREMENTS §4.1 cases 38–44).
 * Runs after s11 and BEFORE s10 (`scenario.config.ts` lists it in the `scenario` project; `scn.STEPS`
 * carries the same order for `SCENARIO_RESUME_FROM`). Administrator only — the registry is hub-global.
 *
 *   a posture    GET /api/settings/model-providers 200: Bedrock configured; SiliconFlow present only on a
 *                ledger seeded before R13 or added by an administrator (N13-obs7 — observation either way),
 *                catalog_version ≥ 1, Titan v2 embedding row;
 *                `/api/capabilities` carries `catalog_version` + `embedding {titan, 1024, bedrock}`; the member
 *                gets 403 on the API and the forbidden card on the page; no `e2es-` leftovers
 *   b provider   add an OpenAI-compatible provider `e2es-mp-<stamp>` on the operator allowlist (no credential)
 *                → 201 with derived names; PUT label → 200; host outside the allowlist → 422; duplicate → 409;
 *                probe → `failed` with a sanitised `credential_unavailable` error (no key material anywhere)
 *   c models     POST chat row with probe:false → 201 `source: manual` + kind defaults; PUT nested
 *                inference_params → `manual` per field; default flip between the two Bedrock chat rows
 *                (capabilities follow; unsetting the default → 409; an unconfigured row → 422); disable →
 *                hidden from capabilities while the registry still lists it → re-enable
 *   d workspace  PUT /workspaces/default/models disabled_models=[row] → echo; capabilities omit it; revert
 *   e journal    GET /changes shows every write above, newest first, in order; `before=<id>` cursor paging
 *   f embedding  POST embedding row (probe:false, dimension 768) → 201; PUT is_default → 409
 *                `embedding.reindex_required` naming the indices while the substrate holds vectors, OR 200 on
 *                an empty substrate (then flipped back) — one of the two, consistent with
 *                `capabilities.embedding` afterwards
 *   g cleanup    DELETE the two rows and the provider → 204; registry and capabilities back to the s12a
 *                baseline; no `e2es` in `warnings[]`
 *
 * Everything is driven through the admin API (the page is screenshotted for evidence); nothing touches AWS —
 * no credential is ever registered, so no Identity provider is created (C8). Names: provider
 * `e2es-mp-<stamp>` (`^[a-z][a-z0-9-]{1,31}$`), rows `e2es-mp-<stamp>.<api_model_name>`; the cleanup spec
 * removes them first when a run dies mid-step.
 */
test.describe.configure({ mode: "serial" });

const MP = "/api/settings/model-providers";
const PAGE = "/v2/settings/model-providers";
const ALLOWED_BASE_URL = "https://api.siliconflow.cn/e2es/v1"; // host on the default operator allowlist
const FORBIDDEN_BASE_URL = "https://models.example.com/v1";
const GLM = "global.zai.glm-5.3";
const KIMI = "global.moonshotai.kimi-k3";
const TITAN = "amazon.titan-embed-text-v2:0";
const CHAT_API_NAME = "e2es/Chat-7B";
const EMBED_API_NAME = "e2es/Embed-768";
const EMBED_DIM = 768;

interface Envelope {
  code?: string;
  message?: string;
  detail?: Record<string, unknown> | null;
}
interface ProbeOut {
  state: string;
  checked_at: string | null;
  latency_ms: number | null;
  models_seen: string[];
  error: string | null;
}
interface ProviderOut {
  id: string;
  name: string;
  kind: string;
  label: string;
  base_url: string | null;
  model_region: string | null;
  credential_provider: string;
  credential_ssm_parameter: string | null;
  credential_source: string;
  credential_env: string | null;
  required: boolean;
  builtin: boolean;
  enabled: boolean;
  id_prefix: string;
  probe: ProbeOut;
  configured: boolean;
  key_version: number | null;
  model_count: number;
  [k: string]: unknown;
}
interface ModelOut {
  id: string;
  provider_id: string;
  provider: string;
  model_id: string;
  api_model_name: string;
  label: string;
  kind: string;
  enabled: boolean;
  is_default: boolean;
  tools: boolean;
  streaming: boolean;
  vision: boolean;
  inference_params: { temperature: boolean; top_p: boolean };
  default_max_tokens: number;
  max_output_tokens: number | null;
  default_judge_max_tokens: number | null;
  embedding_dimension: number | null;
  embedding_max_chars: number | null;
  source: string;
  capability_sources: Record<string, string>;
  [k: string]: unknown;
}
interface RegistryOut {
  catalog_version: number;
  providers: ProviderOut[];
  models: ModelOut[];
  workspaces: Array<{ id: string; name: string; disabled_models: string[] }>;
  warnings: Array<{ code: string; message: string; detail?: Record<string, unknown> | null }>;
}
interface ChangeOut {
  id: number;
  operation: string;
  actor: string;
  target_id: string | null;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  created_at: string;
}
interface Capabilities {
  features: string[];
  catalog_version: number;
  embedding: { model_id: string; dimension: number; provider: string } | null;
  models: Array<{ model_id: string; default: boolean; available: boolean; kind: string; source: string; enabled: boolean; tools: boolean; [k: string]: unknown }>;
  model_providers: Record<string, { configured: boolean; enabled: boolean; probe_state: string; id: string | null; [k: string]: unknown }>;
}

/** Anything that looks like key material — the probe sanitiser replaces secrets with `[redacted]`, which never matches. */
const SECRET_RE = /sk-[A-Za-z0-9]{8,}|Bearer\s+[A-Za-z0-9._-]{8,}|api_key=/i;

function assertNoSecrets(label: string, ...bodies: unknown[]): void {
  for (const body of bodies) {
    const text = typeof body === "string" ? body : JSON.stringify(body);
    expect(SECRET_RE.test(text), `${label}: no key material in the response text`).toBe(false);
  }
}

async function registry(api: APIRequestContext): Promise<RegistryOut> {
  const res = await api.get(MP, { timeout: 60_000 });
  expect(res.status(), `GET ${MP} → ${res.status()}`).toBe(200);
  return (await res.json()) as RegistryOut;
}

async function capabilities(api: APIRequestContext): Promise<Capabilities> {
  const res = await api.get("/api/capabilities", { timeout: 60_000 });
  expect(res.ok(), `GET /api/capabilities → ${res.status()}`).toBeTruthy();
  return (await res.json()) as Capabilities;
}

async function changes(api: APIRequestContext, query: string): Promise<ChangeOut[]> {
  const res = await api.get(`${MP}/changes?${query}`, { timeout: 60_000 });
  expect(res.status(), `GET ${MP}/changes?${query} → ${res.status()}`).toBe(200);
  return ((await res.json()) as { items: ChangeOut[] }).items;
}

async function call(api: APIRequestContext, method: "post" | "put" | "delete", url: string, body: Record<string, unknown> | undefined, label: string): Promise<{ status: number; body: Record<string, unknown>; text: string }> {
  const res = await api[method](url, body === undefined ? { timeout: 60_000 } : { data: body, timeout: 60_000 });
  const text = await res.text();
  let parsed: Record<string, unknown> = {};
  try {
    parsed = text ? (JSON.parse(text) as Record<string, unknown>) : {};
  } catch {
    parsed = { raw: text };
  }
  ledger(`s12 ${label} ${method.toUpperCase()} ${url} ${body === undefined ? "" : JSON.stringify(body).slice(0, 120)} → ${res.status()} ${text.slice(0, 160).replace(/\n/g, " ")}`);
  return { status: res.status(), body: parsed, text };
}

const scnProviders = (reg: RegistryOut) => reg.providers.filter((p) => isScn(p.name)).map((p) => p.name);
const scnModels = (reg: RegistryOut) => reg.models.filter((m) => isScn(m.model_id)).map((m) => m.model_id);

async function workspaceId(api: APIRequestContext): Promise<string> {
  const ws = (await (await api.get("/api/workspaces", { timeout: 60_000 })).json()) as { workspaces: Array<{ id: string; is_default: boolean }> };
  const def = ws.workspaces.find((w) => w.is_default);
  expect(def, "default workspace").toBeTruthy();
  return def!.id;
}

test("s12a — posture: registry readable by the admin (Bedrock configured, Titan v2 embedding, catalog_version ≥ 1), capabilities carry catalog_version + embedding, member → 403 + forbidden card, no e2es leftovers", async ({ page, browser }, testInfo) => {
  stepGate("s12");
  test.setTimeout(10 * 60 * 1000);
  const member = stateRequire("member", "s00");
  const api = page.request;
  const baseURL = testInfo.project.use.baseURL as string;

  const reg = await registry(api);
  await record(testInfo, "registry-before.json", reg, `v${reg.catalog_version} providers=${reg.providers.map((p) => `${p.name}:${p.configured ? "configured" : "unconfigured"}`).join(",")} models=${reg.models.length} warnings=${reg.warnings.map((w) => w.code).join(",") || "none"}`);
  expect(reg.catalog_version, "the registry is seeded (journal id ≥ 1)").toBeGreaterThanOrEqual(1);
  const bedrock = reg.providers.find((p) => p.name === "bedrock");
  expect(bedrock, "the built-in Bedrock provider row").toBeTruthy();
  expect(bedrock!.kind).toBe("bedrock");
  expect(bedrock!.builtin && bedrock!.required && bedrock!.enabled, "Bedrock is built in, required, enabled").toBe(true);
  expect(bedrock!.configured, "Bedrock is configured on the hub (host env bearer)").toBe(true);
  expect(bedrock!.credential_provider).toBe("launchpad-global-bedrock");
  // R13 (N13-obs7): a fresh seed carries no SiliconFlow provider — an administrator adds it with
  // the SSM parameter NAME; a ledger seeded before R13 still has the built-in row. Either way is fine.
  const sf = reg.providers.find((p) => p.name === "siliconflow");
  if (sf) {
    expect(sf.kind).toBe("openai_compatible");
    expect(sf.required, "SiliconFlow is optional").toBe(false);
    expect(sf.credential_ssm_parameter).toBe("launchpad-siliconflow-api-key");
    testInfo.annotations.push({ type: "observation", description: `SiliconFlow provider present (builtin=${String(sf.builtin)} — ${sf.builtin ? "pre-R13 seed" : "admin-registered"})` });
  } else {
    testInfo.annotations.push({ type: "observation", description: "no SiliconFlow provider: R13 seed is Bedrock-only; an administrator may add one via POST /api/settings/model-providers" });
  }
  const titan = reg.models.find((m) => m.model_id === TITAN);
  expect(titan, "the Titan v2 embedding row").toBeTruthy();
  expect(titan!.kind).toBe("embedding");
  expect(titan!.is_default && titan!.enabled, "Titan v2 is the enabled default embedding row").toBe(true);
  expect(titan!.embedding_dimension).toBe(1024);
  expect(titan!.source).toBe("builtin");
  const glm = reg.models.find((m) => m.model_id === GLM);
  expect(glm?.is_default, `${GLM} is the default chat row`).toBe(true);
  expect(reg.models.find((m) => m.model_id === KIMI)?.tools, `${KIMI} supports tools (eligible as a default)`).toBe(true);
  const leftovers = { providers: scnProviders(reg), models: scnModels(reg) };
  ledger(`s12a leftovers ${JSON.stringify(leftovers)}`);
  expect(leftovers.providers, "a fresh environment carries no e2es- provider (run the cleanup first)").toEqual([]);
  expect(leftovers.models, "no e2es- model row").toEqual([]);
  assertNoSecrets("registry", reg);

  const caps = await capabilities(api);
  await record(testInfo, "capabilities-before.json", { catalog_version: caps.catalog_version, embedding: caps.embedding, models: caps.models.map((m) => `${m.model_id}${m.default ? "*" : ""}${m.available ? "" : " (unavailable)"}`), providers: caps.model_providers }, `v${caps.catalog_version} embedding=${JSON.stringify(caps.embedding)} models=${caps.models.length}`);
  expect(caps.catalog_version, "capabilities carry the registry's catalog_version").toBe(reg.catalog_version);
  expect(caps.embedding, "capabilities.embedding is the active Titan v2 row").toEqual({ model_id: TITAN, dimension: 1024, provider: "bedrock" });
  expect(caps.models.length, "capabilities list chat rows").toBeGreaterThanOrEqual(2);
  for (const m of caps.models) {
    expect(m.kind, `${m.model_id} is a chat row`).toBe("chat");
    expect(m.enabled, `${m.model_id} listed ⇒ enabled`).toBe(true);
  }
  expect(caps.models.find((m) => m.model_id === GLM)?.default, "GLM 5.3 is the default in capabilities").toBe(true);
  expect(caps.models.some((m) => m.model_id === TITAN), "the embedding row is never a picker row").toBe(false);
  expect(caps.model_providers.bedrock?.configured, "model_providers.bedrock.configured").toBe(true);
  expect(caps.model_providers.bedrock?.id, "model_providers.bedrock.id is the registry row").toBe(bedrock!.id);

  // SiliconFlow key (plan §11.3 "step 3 once the SiliconFlow key exists") — observation either way
  if (sf?.configured) {
    const probe = await call(api, "post", `${MP}/${sf!.id}/probe`, {}, "s12a siliconflow probe");
    await record(testInfo, "siliconflow-probe.json", probe.body, `state=${String(probe.body.state)} models_seen=${JSON.stringify(probe.body.models_seen).slice(0, 200)}`);
    assertNoSecrets("siliconflow probe", probe.text);
    testInfo.annotations.push({ type: "observation", description: `SiliconFlow key present (configured=true): probe state=${String(probe.body.state)} models_seen=${(probe.body.models_seen as string[] | undefined)?.length ?? 0} error=${String(probe.body.error ?? "")}` });
  } else if (sf) {
    expect(caps.model_providers.siliconflow?.configured, "capabilities agree the SiliconFlow key is absent").toBe(false);
    testInfo.annotations.push({ type: "observation", description: "SiliconFlow key absent (launchpad-siliconflow-api-key not registered) — plan §11.3 step 3 (register credential → probe → add Qwen with probe) deferred until the user creates the parameter" });
  } else {
    expect(caps.model_providers.siliconflow, "capabilities carry no SiliconFlow provider either").toBeUndefined();
  }

  await page.goto(PAGE);
  await expect(page.getByTestId("v2-mp-providers"), "Settings → Model Providers renders for the admin").toBeVisible({ timeout: 60_000 });
  await snap(page, "s12a-providers", `v${reg.catalog_version} ${reg.providers.length} providers`, { key: true });

  // the member: 403 on every route, the forbidden card on the page
  const { page: mp, context: mc } = await loginFresh(browser, member.username, member.password, baseURL);
  const memberGet = await mp.request.get(MP, { timeout: 60_000 });
  const memberBody = (await memberGet.json().catch(() => ({}))) as Envelope;
  ledger(`s12a member GET ${MP} → ${memberGet.status()} ${memberBody.code ?? ""}`);
  expect(memberGet.status(), "a member cannot read the registry").toBe(403);
  const memberPost = await mp.request.post(MP, { data: { name: `${SCN_PREFIX}member-${runStamp()}`, kind: "openai_compatible", label: "x", base_url: ALLOWED_BASE_URL }, timeout: 60_000 });
  expect(memberPost.status(), "a member cannot create a provider").toBe(403);
  await mp.goto(PAGE);
  await expect(mp.getByTestId("v2-mp-forbidden"), "the member sees the admin-required card").toBeVisible({ timeout: 60_000 });
  expect(await mp.getByTestId("v2-mp-providers").count(), "no provider card for the member").toBe(0);
  await snap(mp, "s12a-member-forbidden", undefined, { key: true });
  await mc.close();

  const journal = await changes(api, "limit=1");
  stateSet("model_providers", {
    baseline_version: reg.catalog_version,
    baseline_models: caps.models.map((m) => m.model_id),
    baseline_provider_names: reg.providers.map((p) => p.name),
    baseline_row_count: reg.models.length,
    journal_before: journal[0]?.id ?? 0,
    sf_configured: sf?.configured ?? false, // false also when no SiliconFlow row exists (R13 seed)
  });
  ledger(`s12a PASS v${reg.catalog_version} bedrock configured, siliconflow=${sf ? `configured=${sf.configured}` : "absent (R13 seed)"}, member 403, journal head=${journal[0]?.id ?? 0}`);
});

test("s12b — provider lifecycle: add an OpenAI-compatible provider without a credential → 201 with derived names; PUT label → 200; host outside the allowlist → 422; duplicate → 409; probe → failed with a sanitised error", async ({ page }, testInfo) => {
  stepGate("s12");
  test.setTimeout(10 * 60 * 1000);
  const st = stateRequire("model_providers", "s12");
  const api = page.request;
  const name = `${SCN_PREFIX}mp-${runStamp()}`;
  const upper = name.toUpperCase().replace(/-/g, "_");

  const created = await call(api, "post", MP, { name, kind: "openai_compatible", label: "E2E scenario provider", base_url: ALLOWED_BASE_URL }, "s12b create");
  await record(testInfo, "provider-created.json", created.body, `→ ${created.status} id=${String(created.body.id)}`);
  expect(created.status, "POST provider → 201").toBe(201);
  const prov = created.body as unknown as ProviderOut;
  expect(prov.name).toBe(name);
  expect(prov.kind).toBe("openai_compatible");
  expect(prov.base_url).toBe(ALLOWED_BASE_URL);
  expect(prov.credential_provider, "Identity provider name derived").toBe(`launchpad-${name}`);
  expect(prov.credential_ssm_parameter, "SSM parameter name derived").toBe(`launchpad-${name}-api-key`);
  expect(prov.credential_env, "host env variable derived").toBe(`LAUNCHPAD_${upper}_API_KEY`);
  expect(prov.credential_source).toBe("ssm");
  expect(prov.id_prefix, "Launchpad ids are prefixed with the provider name").toBe(`${name}.`);
  expect(prov.configured, "no credential yet").toBe(false);
  expect(prov.builtin).toBe(false);
  expect(prov.enabled).toBe(true);
  expect(prov.probe.state).toBe("never");
  expect(prov.model_count).toBe(0);
  expect(prov.key_version, "no key version before a credential is registered").toBeNull();
  assertNoSecrets("provider create", created.text);

  const byName = await api.get(`${MP}/${encodeURIComponent(name)}`, { timeout: 60_000 });
  expect(byName.status(), "GET /{name} resolves the row").toBe(200);
  expect(((await byName.json()) as ProviderOut).id).toBe(prov.id);

  const edited = await call(api, "put", `${MP}/${prov.id}`, { label: "E2E scenario provider (edited)" }, "s12b edit");
  expect(edited.status, "PUT label → 200").toBe(200);
  expect(edited.body.label).toBe("E2E scenario provider (edited)");
  expect(edited.body.name, "name is immutable").toBe(name);

  // refusals: host outside the operator allowlist (region boundary for data the lint cannot see), duplicate name
  const forbidden = await call(api, "post", MP, { name: `${name}-x`, kind: "openai_compatible", label: "x", base_url: FORBIDDEN_BASE_URL }, "s12b host-not-allowed");
  await record(testInfo, "host-not-allowed.json", forbidden.body, `→ ${forbidden.status} ${String(forbidden.body.code ?? "")}`);
  expect(forbidden.status, "a base_url outside the allowlist is refused").toBe(422);
  expect(forbidden.body.code).toBe("model_provider.host_not_allowed");
  const duplicate = await call(api, "post", MP, { name, kind: "openai_compatible", label: "dup", base_url: ALLOWED_BASE_URL }, "s12b duplicate");
  expect(duplicate.status, "a duplicate name is refused").toBe(409);
  expect(duplicate.body.code).toBe("model_provider.exists");

  // probe without a credential: failed, sanitised, nothing left the host
  const probe = await call(api, "post", `${MP}/${prov.id}/probe`, {}, "s12b probe");
  await record(testInfo, "provider-probe.json", probe.body, `→ ${probe.status} state=${String(probe.body.state)} error=${String(probe.body.error ?? "").slice(0, 120)}`);
  expect(probe.status, "POST /probe answers 200 with the result").toBe(200);
  const pr = probe.body as unknown as ProbeOut;
  expect(pr.state, "no credential ⇒ failed").toBe("failed");
  expect(String(pr.error ?? ""), "the error names the missing credential").toMatch(/credential_unavailable/);
  expect(pr.models_seen).toEqual([]);
  expect(String(pr.error ?? "").length, "sanitised error ≤ 300 chars").toBeLessThanOrEqual(300);
  const after = (await (await api.get(`${MP}/${prov.id}`, { timeout: 60_000 })).json()) as ProviderOut;
  expect(after.probe.state, "the probe result is stored on the row").toBe("failed");
  expect(after.configured).toBe(false);
  const reg = await registry(api);
  softly(testInfo, "warnings[] surface the failed probe", () => expect(reg.warnings.some((w) => w.code === "probe_failed")).toBe(true));
  assertNoSecrets("probe", probe.text, after, reg);

  await page.goto(`${PAGE}?provider=${encodeURIComponent(prov.id)}`);
  await expect(page.getByTestId("v2-mp-providers")).toBeVisible({ timeout: 60_000 });
  await snap(page, "s12b-provider", `${name} probe=${pr.state}`, { key: true });

  stateSet("model_providers", { ...st, provider_id: prov.id, provider_name: name });
  ledger(`s12b PASS provider ${name}=${prov.id} probe=${pr.state}`);
});

test("s12c — model rows: POST chat row (probe:false) → 201 manual + kind defaults; PUT nested inference_params → manual per field; default flips between the Bedrock rows and capabilities follow; disable hides the row from capabilities while the registry lists it", async ({ page }, testInfo) => {
  stepGate("s12");
  test.setTimeout(10 * 60 * 1000);
  const st = stateRequire("model_providers", "s12");
  if (!st.provider_id) throw new Error("scenario state 'model_providers.provider_id' missing — run s12b first");
  const api = page.request;
  const pid = st.provider_id;

  const created = await call(api, "post", `${MP}/${pid}/models`, { api_model_name: CHAT_API_NAME, kind: "chat", probe: false, label: "E2E scenario chat row" }, "s12c create chat");
  await record(testInfo, "model-created.json", created.body, `→ ${created.status} ${String(created.body.model_id)} source=${String(created.body.source)}`);
  expect(created.status, "POST model (probe:false) → 201").toBe(201);
  const row = created.body as unknown as ModelOut;
  expect(row.model_id, "Launchpad id = id_prefix + api_model_name").toBe(`${st.provider_name}.${CHAT_API_NAME}`);
  expect(row.kind).toBe("chat");
  expect(row.source, "no probe ⇒ manual").toBe("manual");
  expect(row.enabled && !row.is_default).toBe(true);
  expect(row.tools && row.streaming, "OpenAI-compatible chat defaults: tools + streaming on").toBe(true);
  expect(row.inference_params).toEqual({ temperature: true, top_p: true });
  expect(row.default_max_tokens, "OpenAI-compatible default output ceiling").toBe(8192);
  expect(row.default_judge_max_tokens, "OpenAI-compatible judge budget").toBe(4096);
  expect(new Set(Object.values(row.capability_sources)), "every tracked field is manual").toEqual(new Set(["manual"]));
  const dup = await call(api, "post", `${MP}/${pid}/models`, { api_model_name: CHAT_API_NAME, kind: "chat", probe: false }, "s12c duplicate");
  expect(dup.status, "the same api_model_name twice → 409").toBe(409);
  expect(dup.body.code).toBe("model.exists");

  const edited = await call(api, "put", `${MP}/${pid}/models/${row.id}`, { inference_params: { temperature: false }, default_max_tokens: 4096 }, "s12c edit params");
  await record(testInfo, "model-edited.json", edited.body, `→ ${edited.status} temperature=${String((edited.body.inference_params as { temperature?: boolean } | undefined)?.temperature)}`);
  expect(edited.status).toBe(200);
  const ed = edited.body as unknown as ModelOut;
  expect(ed.inference_params, "nested inference_params PUT applies one field and keeps the other").toEqual({ temperature: false, top_p: true });
  expect(ed.default_max_tokens).toBe(4096);
  expect(ed.capability_sources.temperature).toBe("manual");
  expect(ed.capability_sources.default_max_tokens).toBe("manual");
  expect(ed.source).toBe("manual");
  const slashed = await call(api, "put", `${MP}/${pid}/models/${row.model_id}`, { label: "E2E scenario chat row (by Launchpad id)" }, "s12c edit by slashed id");
  expect(slashed.status, "model_ref accepts the slashed Launchpad id").toBe(200);
  expect(slashed.body.id).toBe(row.id);

  let caps = await capabilities(api);
  const mine = caps.models.find((m) => m.model_id === row.model_id);
  expect(mine, "the new chat row reaches /api/capabilities").toBeTruthy();
  expect(mine!.available, "its provider has no key ⇒ listed but unavailable").toBe(false);
  expect(mine!.source).toBe("manual");
  expect(mine!.kind).toBe("chat");

  // default: refused on an unconfigured row (422), flipped between the two Bedrock rows, unsetting refused (409)
  const badDefault = await call(api, "put", `${MP}/${pid}/models/${row.id}`, { is_default: true }, "s12c default on unconfigured row");
  expect(badDefault.status, "a row whose provider is unconfigured cannot become the default").toBe(422);
  expect(badDefault.body.code).toBe("model.default_invalid");
  const reg = await registry(api);
  const bedrock = reg.providers.find((p) => p.name === "bedrock")!;
  const glm = reg.models.find((m) => m.model_id === GLM)!;
  const kimi = reg.models.find((m) => m.model_id === KIMI)!;
  expect(glm.is_default && !kimi.is_default, "GLM 5.3 is the default before the flip").toBe(true);
  const toKimi = await call(api, "put", `${MP}/${bedrock.id}/models/${kimi.id}`, { is_default: true }, "s12c default → kimi");
  expect(toKimi.status, "PUT is_default on an enabled, tool-capable, configured chat row → 200").toBe(200);
  expect(toKimi.body.is_default).toBe(true);
  caps = await capabilities(api);
  expect(caps.models.find((m) => m.model_id === KIMI)?.default, "capabilities: Kimi K3 is the default").toBe(true);
  expect(caps.models.find((m) => m.model_id === GLM)?.default, "capabilities: GLM 5.3 no longer default").toBe(false);
  const unset = await call(api, "put", `${MP}/${bedrock.id}/models/${kimi.id}`, { is_default: false }, "s12c unset default");
  expect(unset.status, "unsetting the default is refused").toBe(409);
  expect(unset.body.code).toBe("model.is_default");
  const toGlm = await call(api, "put", `${MP}/${bedrock.id}/models/${glm.id}`, { is_default: true }, "s12c default → glm");
  expect(toGlm.status).toBe(200);
  caps = await capabilities(api);
  expect(caps.models.find((m) => m.model_id === GLM)?.default, "capabilities: GLM 5.3 is the default again").toBe(true);
  expect(caps.models.find((m) => m.model_id === KIMI)?.default).toBe(false);

  // disable → omitted from capabilities, still a registry row (stored specs keep parsing — hermetic
  // `test_disabled_model_is_hidden_from_pickers_but_stored_specs_parse`); re-enable → listed again
  const disabled = await call(api, "put", `${MP}/${pid}/models/${row.id}`, { enabled: false }, "s12c disable");
  expect(disabled.status).toBe(200);
  expect(disabled.body.enabled).toBe(false);
  caps = await capabilities(api);
  expect(caps.models.some((m) => m.model_id === row.model_id), "a disabled row is omitted from capabilities").toBe(false);
  const regDisabled = await registry(api);
  const still = regDisabled.models.find((m) => m.model_id === row.model_id);
  expect(still, "the registry still lists the disabled row").toBeTruthy();
  expect(still!.enabled).toBe(false);
  expect(regDisabled.catalog_version, "every write advances the catalog version").toBeGreaterThan(reg.catalog_version);
  const enabled = await call(api, "put", `${MP}/${pid}/models/${row.id}`, { enabled: true }, "s12c enable");
  expect(enabled.status).toBe(200);
  caps = await capabilities(api);
  expect(caps.models.some((m) => m.model_id === row.model_id), "re-enabled ⇒ listed again").toBe(true);
  ledger("s12c note: 409 model.in_use needs an agent / evaluator on the row (its provider is unconfigured, so none can be created here) — covered hermetically by test_delete_model_rules_and_referrers; skipped live");

  await page.goto(`${PAGE}?provider=${encodeURIComponent(pid)}`);
  await expect(page.getByTestId("v2-mp-providers")).toBeVisible({ timeout: 60_000 });
  await snap(page, "s12c-model-table", `${row.model_id} manual`, { key: true });

  stateSet("model_providers", { ...st, chat_row_id: row.id, chat_model_id: row.model_id });
  ledger(`s12c PASS ${row.model_id}=${row.id} manual; default kimi→glm round trip; disable/enable`);
});

test("s12d — workspace flags: PUT /workspaces/default/models disabled_models=[row] → echo; capabilities omit it; unknown id → 422; revert", async ({ page }, testInfo) => {
  stepGate("s12");
  test.setTimeout(10 * 60 * 1000);
  const st = stateRequire("model_providers", "s12");
  if (!st.chat_model_id) throw new Error("scenario state 'model_providers.chat_model_id' missing — run s12c first");
  const api = page.request;
  const ws = await workspaceId(api);

  const flagged = await call(api, "put", `${MP}/workspaces/${ws}/models`, { disabled_models: [st.chat_model_id] }, "s12d flag");
  await record(testInfo, "workspace-flags.json", flagged.body, `→ ${flagged.status} ${JSON.stringify(flagged.body.disabled_models)}`);
  expect(flagged.status).toBe(200);
  expect(flagged.body.id, "echo carries the workspace id").toBe(ws);
  expect(typeof flagged.body.name).toBe("string");
  expect(flagged.body.disabled_models).toEqual([st.chat_model_id]);
  expect(flagged.body.workspace_id).toBe(ws);
  let caps = await capabilities(api);
  expect(caps.models.some((m) => m.model_id === st.chat_model_id), "a workspace-disabled row is omitted from this workspace's capabilities").toBe(false);
  const reg = await registry(api);
  expect(reg.workspaces.find((w) => w.id === ws)?.disabled_models, "the registry page lists the flag").toEqual([st.chat_model_id]);
  expect(reg.models.find((m) => m.model_id === st.chat_model_id)?.enabled, "the hub-wide row stays enabled (the flag is per workspace)").toBe(true);

  const unknown = await call(api, "put", `${MP}/workspaces/${ws}/models`, { disabled_models: ["nope.not-a-model"] }, "s12d unknown");
  expect(unknown.status, "an unknown model id is refused").toBe(422);
  expect(unknown.body.code).toBe("model.unknown");

  await page.goto(`${PAGE}?view=workspaces`);
  await expect(page.getByTestId("v2-mp-matrix"), "workspace × model matrix").toBeVisible({ timeout: 60_000 });
  await snap(page, "s12d-workspace-matrix", `${ws} disables ${st.chat_model_id}`, { key: true });

  const reverted = await call(api, "put", `${MP}/workspaces/${ws}/models`, { disabled_models: [] }, "s12d revert");
  expect(reverted.status).toBe(200);
  expect(reverted.body.disabled_models).toEqual([]);
  caps = await capabilities(api);
  expect(caps.models.some((m) => m.model_id === st.chat_model_id), "reverted ⇒ listed again").toBe(true);

  stateSet("model_providers", { ...st, workspace_id: ws });
  ledger(`s12d PASS workspace ${ws} flag set + reverted`);
});

test("s12e — changes journal: every write above is journaled newest first, in order, with before/after and no key material; `before=<id>` cursor paging walks the same set", async ({ page }, testInfo) => {
  stepGate("s12");
  test.setTimeout(10 * 60 * 1000);
  const st = stateRequire("model_providers", "s12");
  if (!st.provider_id || !st.chat_row_id || !st.workspace_id) throw new Error("scenario state 'model_providers' incomplete — run s12b–s12d first");
  const api = page.request;

  const all = (await changes(api, "limit=200")).filter((c) => c.id > st.journal_before);
  await record(testInfo, "changes.json", all, `${all.length} rows since #${st.journal_before}: ${all.map((c) => `${c.id}:${c.operation}`).reverse().join(" ")}`);
  expect(all.length, "the s12 writes are journaled").toBeGreaterThanOrEqual(11);
  for (let i = 1; i < all.length; i++) expect(all[i - 1].id, "newest first, ids strictly decreasing").toBeGreaterThan(all[i].id);
  for (const c of all) {
    expect(typeof c.created_at).toBe("string");
    expect(c.actor, "every row names its actor").toBeTruthy();
  }
  assertNoSecrets("journal", all);
  const oldestFirst = [...all].reverse();
  const ops = (pred: (c: ChangeOut) => boolean) => oldestFirst.filter(pred).map((c) => c.operation);
  expect(ops((c) => c.target_id === st.provider_id), "provider: create → update → probe").toEqual(["provider.create", "provider.update", "provider.probe"]);
  const modelOps = ops((c) => c.target_id === st.chat_row_id);
  expect(modelOps[0], "the chat row starts with model.create").toBe("model.create");
  expect(modelOps.filter((o) => o === "model.update").length, "params, label, disable, enable → 4 model.update rows").toBe(4);
  expect(ops((c) => c.operation === "workspace.flags" && c.target_id === st.workspace_id), "two workspace.flags rows (set, revert)").toEqual(["workspace.flags", "workspace.flags"]);
  const bedrockDefaults = oldestFirst.filter((c) => c.operation === "model.update" && [GLM, KIMI].includes(String(c.after?.model_id ?? "")));
  expect(bedrockDefaults.map((c) => String(c.after?.model_id)), "the two default flips are journaled in order (kimi, then glm)").toEqual([KIMI, GLM]);
  const createRow = oldestFirst.find((c) => c.operation === "provider.create" && c.target_id === st.provider_id)!;
  expect(createRow.before, "create: before is null").toBeNull();
  expect(createRow.after?.name).toBe(st.provider_name);
  const disableRow = oldestFirst.find((c) => c.target_id === st.chat_row_id && c.operation === "model.update" && c.before?.enabled === true && c.after?.enabled === false);
  expect(disableRow, "the disable write records before.enabled=true → after.enabled=false").toBeTruthy();
  // the refused writes (422 host_not_allowed / default_invalid / model.unknown, 409 exists / is_default) left no row
  expect(oldestFirst.some((c) => String(c.after?.base_url ?? "") === FORBIDDEN_BASE_URL), "a refused provider is never journaled").toBe(false);

  // cursor paging: limit=4 + before=<last id> walks exactly the same set with no overlap
  const walked: ChangeOut[] = [];
  let before: number | undefined;
  for (let i = 0; i < 50; i++) {
    const pageRows = await changes(api, `limit=4${before === undefined ? "" : `&before=${before}`}`);
    if (!pageRows.length) break;
    for (const r of pageRows) expect(before === undefined || r.id < before, `before=${before}: every id is older`).toBe(true);
    walked.push(...pageRows);
    before = pageRows[pageRows.length - 1].id;
    if (before <= st.journal_before) break;
  }
  const walkedSince = walked.filter((c) => c.id > st.journal_before);
  expect(new Set(walkedSince.map((c) => c.id)).size, "no duplicates across pages").toBe(walkedSince.length);
  expect(walkedSince.map((c) => c.id), "cursor paging yields the same rows as one large page").toEqual(all.map((c) => c.id));

  await page.goto(`${PAGE}?view=changes`);
  await page.waitForLoadState("networkidle").catch(() => undefined);
  await snap(page, "s12e-changes", `${all.length} rows since #${st.journal_before}`, { key: true });
  ledger(`s12e PASS ${all.length} journal rows, paging ok`);
});

test("s12f — embedding guard: POST embedding row (probe:false, dimension 768) → 201; PUT is_default → 409 embedding.reindex_required naming the indices while vectors exist, or 200 on an empty substrate (then flipped back); capabilities.embedding agrees", async ({ page }, testInfo) => {
  stepGate("s12");
  test.setTimeout(10 * 60 * 1000);
  const st = stateRequire("model_providers", "s12");
  if (!st.provider_id) throw new Error("scenario state 'model_providers.provider_id' missing — run s12b first");
  const api = page.request;
  const pid = st.provider_id;

  const noDim = await call(api, "post", `${MP}/${pid}/models`, { api_model_name: EMBED_API_NAME, kind: "embedding", probe: false }, "s12f embedding without dimension");
  expect(noDim.status, "an embedding row without a dimension (no probe) is refused").toBe(422);
  expect(noDim.body.code).toBe("model.invalid");
  const created = await call(api, "post", `${MP}/${pid}/models`, { api_model_name: EMBED_API_NAME, kind: "embedding", probe: false, overrides: { embedding_dimension: EMBED_DIM } }, "s12f create embedding");
  await record(testInfo, "embedding-created.json", created.body, `→ ${created.status} ${String(created.body.model_id)} dim=${String(created.body.embedding_dimension)}`);
  expect(created.status).toBe(201);
  const row = created.body as unknown as ModelOut;
  expect(row.kind).toBe("embedding");
  expect(row.embedding_dimension).toBe(EMBED_DIM);
  expect(row.is_default, "a new embedding row is never the default").toBe(false);
  expect(row.source).toBe("manual");
  const before = await capabilities(api);
  expect(before.embedding, "Titan v2 is still the active embedding").toEqual({ model_id: TITAN, dimension: 1024, provider: "bedrock" });
  expect(before.models.some((m) => m.model_id === row.model_id), "an embedding row never reaches the chat pickers").toBe(false);

  const flip = await call(api, "put", `${MP}/${pid}/models/${row.id}`, { is_default: true }, "s12f default flip");
  await record(testInfo, "embedding-flip.json", flip.body, `→ ${flip.status} ${String(flip.body.code ?? "")} detail=${JSON.stringify(flip.body.detail ?? {}).slice(0, 300)}`);
  let branch: "refused" | "flipped";
  if (flip.status === 409) {
    branch = "refused";
    expect(flip.body.code, "409 names the reindex guard").toBe("embedding.reindex_required");
    const detail = (flip.body.detail ?? {}) as { indices?: Array<{ name: string; docs: number }>; dimension_from?: number; dimension_to?: number; model_from?: string; model_to?: string };
    expect(Array.isArray(detail.indices) && detail.indices.length > 0, "detail.indices names the vector indices").toBe(true);
    expect(detail.indices!.some((i) => Number(i.docs) > 0), "at least one index holds documents").toBe(true);
    for (const i of detail.indices!) expect(typeof i.name === "string" && i.name.length > 0, "every index is named").toBe(true);
    expect(detail.dimension_from).toBe(1024);
    expect(detail.dimension_to).toBe(EMBED_DIM);
    expect(detail.model_from).toBe(TITAN);
    expect(detail.model_to).toBe(row.model_id);
    const after = await capabilities(api);
    expect(after.embedding, "refused ⇒ the active embedding is unchanged").toEqual(before.embedding);
    const still = (await registry(api)).models.find((m) => m.model_id === row.model_id);
    expect(still?.is_default, "the new row did not become the default").toBe(false);
    testInfo.annotations.push({ type: "observation", description: `embedding default flip refused: ${detail.indices!.map((i) => `${i.name}=${i.docs}`).join(", ")} (dimension ${detail.dimension_from} → ${detail.dimension_to})` });
  } else {
    branch = "flipped";
    expect(flip.status, "an empty substrate allows the flip").toBe(200);
    expect(flip.body.is_default).toBe(true);
    const after = await capabilities(api);
    expect(after.embedding, "flipped ⇒ the active embedding is the new row").toEqual({ model_id: row.model_id, dimension: EMBED_DIM, provider: st.provider_name });
    const reg = await registry(api);
    const bedrock = reg.providers.find((p) => p.name === "bedrock")!;
    const titan = reg.models.find((m) => m.model_id === TITAN)!;
    const back = await call(api, "put", `${MP}/${bedrock.id}/models/${titan.id}`, { is_default: true }, "s12f default back to titan");
    expect(back.status, "flipping back on the same empty substrate → 200").toBe(200);
    const restored = await capabilities(api);
    expect(restored.embedding).toEqual({ model_id: TITAN, dimension: 1024, provider: "bedrock" });
    testInfo.annotations.push({ type: "observation", description: "embedding default flip allowed (no vector index holds documents on this environment) — flipped back to Titan v2" });
  }
  await page.goto(`${PAGE}?provider=${encodeURIComponent(pid)}`);
  await expect(page.getByTestId("v2-mp-providers")).toBeVisible({ timeout: 60_000 });
  await snap(page, "s12f-embedding", `${row.model_id} dim ${EMBED_DIM} flip=${branch}`, { key: true });

  stateSet("model_providers", { ...st, embed_row_id: row.id, embed_model_id: row.model_id, embedding_branch: branch });
  ledger(`s12f PASS ${row.model_id} dim=${EMBED_DIM} branch=${branch}`);
});

test("s12g — cleanup: DELETE the two rows and the provider → 204; registry and capabilities back to the s12a baseline; no e2es in warnings[]", async ({ page }, testInfo) => {
  stepGate("s12");
  test.setTimeout(10 * 60 * 1000);
  const st = stateRequire("model_providers", "s12");
  if (!st.provider_id) throw new Error("scenario state 'model_providers.provider_id' missing — run s12b first");
  const api = page.request;
  const pid = st.provider_id;

  const deleted: Array<{ what: string; status: number }> = [];
  for (const [what, rowId] of [["embedding row", st.embed_row_id], ["chat row", st.chat_row_id]] as Array<[string, string | undefined]>) {
    if (!rowId) continue;
    const r = await call(api, "delete", `${MP}/${pid}/models/${rowId}`, undefined, `s12g delete ${what}`);
    deleted.push({ what, status: r.status });
    expect(r.status, `DELETE ${what} → 204`).toBe(204);
  }
  const prov = await call(api, "delete", `${MP}/${pid}`, undefined, "s12g delete provider");
  deleted.push({ what: "provider", status: prov.status });
  expect(prov.status, "DELETE provider → 204").toBe(204);
  const gone = await api.get(`${MP}/${pid}`, { timeout: 60_000 });
  expect(gone.status(), "the provider is gone").toBe(404);
  expect(((await gone.json().catch(() => ({}))) as Envelope).code).toBe("model_provider.not_found");

  const reg = await registry(api);
  const caps = await capabilities(api);
  await record(testInfo, "registry-after.json", { deleted, catalog_version: reg.catalog_version, providers: reg.providers.map((p) => p.name), models: reg.models.map((m) => m.model_id), warnings: reg.warnings, capabilities_models: caps.models.map((m) => m.model_id), embedding: caps.embedding }, `v${reg.catalog_version} providers=${reg.providers.length} models=${reg.models.length} warnings=${reg.warnings.map((w) => w.code).join(",") || "none"}`);
  expect(scnProviders(reg), "no e2es- provider left").toEqual([]);
  expect(scnModels(reg), "no e2es- model row left").toEqual([]);
  expect(reg.providers.map((p) => p.name).sort(), "providers back to the baseline").toEqual([...st.baseline_provider_names].sort());
  expect(reg.models.length, "model rows back to the baseline count").toBe(st.baseline_row_count);
  expect(reg.catalog_version, "the journal only grows").toBeGreaterThan(st.baseline_version);
  expect(JSON.stringify(reg.warnings).includes("e2es"), "no e2es leftover in warnings[]").toBe(false);
  expect(caps.models.map((m) => m.model_id), "capabilities list the baseline chat rows in order").toEqual(st.baseline_models);
  expect(caps.models.find((m) => m.model_id === GLM)?.default, "GLM 5.3 default restored").toBe(true);
  expect(caps.embedding, "Titan v2 is the active embedding").toEqual({ model_id: TITAN, dimension: 1024, provider: "bedrock" });
  expect(caps.catalog_version).toBe(reg.catalog_version);
  expect(reg.workspaces.every((w) => !w.disabled_models.some((m) => isScn(m))), "no e2es workspace flag left").toBe(true);
  const tail = (await changes(api, "limit=5")).map((c) => c.operation);
  expect(tail[0], "the last journal row is the provider delete").toBe("provider.delete");
  ledger("s12g note: no credential was registered, so no Identity provider / SSM read happened — nothing on AWS to tear down (C8)");

  await page.goto(PAGE);
  await expect(page.getByTestId("v2-mp-providers")).toBeVisible({ timeout: 60_000 });
  await snap(page, "s12g-restored", `v${reg.catalog_version} baseline restored`, { key: true });
  stateSet("model_providers", { ...st, cleaned: true });
  ledger(`s12g PASS deleted ${deleted.length} objects; registry v${reg.catalog_version} back to baseline`);
});

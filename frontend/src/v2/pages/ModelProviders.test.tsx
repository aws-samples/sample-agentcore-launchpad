// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../../auth/auth-context";
import { WorkspaceContext } from "../../workspace/workspace-context";
import { ToastContext } from "../hooks";
import { roleHidden } from "./modelProviders/common";
import { ApiError, type ModelDefaultsOut, type ModelProviderOut, type ModelProvidersPage, type ModelRoleSetting, type ModelRowOut, type Workspace } from "../../lib/api";
import { resetModelDefaultsForTests } from "../../lib/model-defaults";
import en from "../../locales/en/common.json";
import zh from "../../locales/zh-CN/common.json";
import { V2_NAV } from "../nav";
import { V2ModelProviders } from "./ModelProviders";

/**
 * D21 §8 / §9 / §10: the Settings → Model Providers page is administrator-only (members
 * get the forbidden card and no request is fired), lists providers with kind / endpoint /
 * credential REFERENCE status, renders the model table with capability chips and the
 * `source` badge, drives enable / edit / probe / workspace-flag writes through the typed
 * client, tolerates the 501 `model_providers.probe_unavailable` of a server without the
 * probe module, renders the 409 referrer list, and shows the change journal.
 */

const bedrock: ModelProviderOut = {
  id: "mp_bedrock", name: "bedrock", kind: "bedrock", label: "Global Bedrock", base_url: null,
  model_region: "us-west-2", credential_provider: "launchpad-global-bedrock", credential_ssm_parameter: null,
  credential_source: "host_env", credential_env: "LAUNCHPAD_GLOBAL_BEDROCK_API_KEY", required: true, builtin: true,
  enabled: true, id_prefix: "", configured: true, scope: "workspace", created_by: null, created_by_name: null, owned: false,
  probe: { state: "never", checked_at: null, latency_ms: null, models_seen: [], error: null },
};
const siliconflow: ModelProviderOut = {
  id: "mp_sf", name: "siliconflow", kind: "openai_compatible", label: "SiliconFlow", base_url: "https://api.siliconflow.cn/v1",
  model_region: null, credential_provider: "launchpad-siliconflow", credential_ssm_parameter: "launchpad-siliconflow-api-key",
  credential_source: "ssm", required: false, builtin: false, enabled: true, id_prefix: "siliconflow.", configured: false,
  scope: "workspace", created_by: null, created_by_name: "ops", owned: false,
  probe: { state: "ok", checked_at: "2026-10-14T08:00:00Z", latency_ms: 420, models_seen: ["Qwen/Qwen3-8B", "zai-org/GLM-5.3"], error: null },
};
// N17-obs203: a member's own personal provider (alice, `u-alice`) and another member's shared one
const ppio: ModelProviderOut = {
  id: "mp_ppio", name: "ppio", kind: "openai_compatible", label: "PPIO", base_url: "https://api.ppinfra.com/v3/openai",
  model_region: null, credential_provider: "launchpad-user-u-alice-ppio", credential_ssm_parameter: "launchpad-user-u-alice-ppio-api-key",
  credential_env: null, credential_source: "ssm", required: false, builtin: false, enabled: true, id_prefix: "ppio.", configured: false,
  scope: "personal", created_by: "u-alice", created_by_name: "alice", owned: true,
  probe: { state: "never", checked_at: null, latency_ms: null, models_seen: [], error: null },
};
const bobShared: ModelProviderOut = {
  ...ppio, id: "mp_bob", name: "bobvendor", label: "Bob vendor", credential_provider: "launchpad-user-u-bob-bobvendor",
  credential_ssm_parameter: "launchpad-user-u-bob-bobvendor-api-key", id_prefix: "bobvendor.",
  scope: "workspace", created_by: "u-bob", created_by_name: "bob", owned: false,
};
const glm: ModelRowOut = {
  id: "md_glm", provider_id: "mp_bedrock", model_id: "global.zai.glm-5.3", api_model_name: "global.zai.glm-5.3",
  label: "GLM 5.3 (Z.AI · global)", kind: "chat", enabled: true, is_default: true, tools: true, streaming: true, vision: false,
  inference_params: { temperature: true, top_p: true }, default_max_tokens: 16384, max_output_tokens: 16384,
  default_judge_max_tokens: 4096, embedding_dimension: null, embedding_max_chars: null, source: "builtin",
  capability_sources: {}, probe: null, sort_order: 0,
};
const kimi: ModelRowOut = {
  ...glm, id: "md_kimi", model_id: "global.moonshotai.kimi-k3", api_model_name: "global.moonshotai.kimi-k3",
  label: "Kimi K3 (Moonshot AI · global)", is_default: false, vision: true,
  inference_params: { temperature: false, top_p: true }, source: "probed",
  capability_sources: { tools: "probed", streaming: "probed", temperature: "probed", top_p: "probed", vision: "manual" },
  probe: { checked_at: "2026-10-14T08:00:00Z", latency_ms: 900, findings: { exists: true, tools: true, streaming: true, temperature: false, top_p: true }, errors: {}, raw_hint: {} },
  sort_order: 1,
};
const titan: ModelRowOut = {
  ...glm, id: "md_titan", model_id: "amazon.titan-embed-text-v2:0", api_model_name: "amazon.titan-embed-text-v2:0",
  label: "Titan Embeddings v2", kind: "embedding", tools: false, streaming: false, is_default: true,
  embedding_dimension: 1024, embedding_max_chars: 8000, sort_order: 2,
};
const qwen: ModelRowOut = {
  ...glm, id: "md_qwen", provider_id: "mp_sf", model_id: "siliconflow.Qwen/Qwen3-8B", api_model_name: "Qwen/Qwen3-8B",
  label: "Qwen3-8B (SiliconFlow)", is_default: false, source: "manual", default_max_tokens: 8192, max_output_tokens: null,
  capability_sources: { tools: "manual", temperature: "unknown" }, sort_order: 3,
};

function pageBody(): ModelProvidersPage {
  return {
    catalog_version: 7,
    providers: [bedrock, siliconflow],
    models: [glm, kimi, titan, qwen],
    workspaces: [
      { id: "default", name: "default", disabled_models: [] },
      { id: "t1", name: "t1", disabled_models: ["global.moonshotai.kimi-k3"] },
    ],
    warnings: [{ code: "probe_failed", message: "x" }],
  };
}

const mp = vi.hoisted(() => ({
  list: vi.fn(async () => pageBody()),
  create: vi.fn(async (body: unknown) => ({ ...siliconflow, id: "mp_new", ...(body as object) })),
  get: vi.fn(),
  update: vi.fn(async (_id: string, body: { enabled: boolean }) => ({ ...siliconflow, enabled: body.enabled })),
  remove: vi.fn(async () => undefined),
  credential: vi.fn(async () => ({ configured: true as const, created: true, rotated: false, workspaces_updated: 2 })),
  probe: vi.fn<(id: string) => Promise<unknown>>(),
  addModel: vi.fn(async () => qwen),
  updateModel: vi.fn(async (_p: string, _m: string, body: object) => ({ ...kimi, ...body })),
  removeModel: vi.fn(async () => undefined),
  probeModel: vi.fn<(p: string, m: string, apply?: boolean) => Promise<unknown>>(),
  setWorkspaceFlags: vi.fn(async (id: string, disabled: string[]) => ({ id, name: id, disabled_models: disabled })),
  changes: vi.fn(async () => ({
    items: [
      { id: 7, operation: "model.update", actor: "ops", target_id: "md_kimi", before: { tools: false }, after: { tools: true }, created_at: "2026-10-14T09:00:00Z" },
      { id: 1, operation: "seed", actor: null, target_id: null, before: null, after: { providers: 2 }, created_at: "2026-10-14T07:00:00Z" },
    ],
  })),
}));
// r15-e: the Model roles tab (`GET /api/model-defaults`, admin PUT / DELETE /api/settings/model-roles/{role})
const GLM_ID = "global.zai.glm-5.3";
const KIMI_ID = "global.moonshotai.kimi-k3";
function roleRow(partial: Partial<ModelRoleSetting> & { role: string }): ModelRoleSetting {
  return {
    field: `${partial.role}_model_id`, model_id: GLM_ID, default: GLM_ID, overridden_by: null, updated_at: null,
    needs_tools: false, allow_empty: false, ...partial,
  };
}
function rolesBody(): ModelDefaultsOut {
  return {
    roles: [
      roleRow({ role: "agent_default", field: "agent_default_model_id", needs_tools: true }),
      roleRow({ role: "judge", field: "eval_judge_model_id", model_id: KIMI_ID, overridden_by: "ops", updated_at: "2026-10-19T08:00:00Z" }),
      roleRow({ role: "judge_fallback", field: "eval_judge_fallback_model_id", model_id: "", default: "", allow_empty: true }),
      roleRow({ role: "actor", field: "eval_actor_model_id", needs_tools: true }),
      roleRow({ role: "prompt_opt", field: "prompt_opt_default_model_id" }),
      roleRow({ role: "skill_lab_judge", field: "skill_lab_judge_model_id", feature: "skill_lab" }),
      roleRow({ role: "skill_lab_target", field: "skill_lab_target_model_id", model_id: KIMI_ID, default: KIMI_ID, needs_tools: true, feature: "skill_lab" }),
      // N17-obs252
      roleRow({ role: "memory_extract", field: "memory_extract_model_id" }),
      roleRow({ role: "codegen", field: "codegen_model", model_id: KIMI_ID, default: KIMI_ID, feature: "studio" }),
      roleRow({ role: "embedding", field: "", model_id: "amazon.titan-embed-text-v2:0", default: "amazon.titan-embed-text-v2:0", kind: "embedding" }),
    ],
  };
}
const roles = vi.hoisted(() => ({
  modelDefaults: vi.fn<() => Promise<unknown>>(),
  set: vi.fn<(role: string, modelId: string) => Promise<unknown>>(),
  reset: vi.fn<(role: string) => Promise<unknown>>(),
}));
vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      modelProviders: mp,
      modelDefaults: roles.modelDefaults,
      modelRoles: { set: roles.set, reset: roles.reset },
    },
  };
});

beforeAll(async () => {
  Element.prototype.scrollIntoView = () => undefined; // jsdom has none; the listbox scrolls its active option
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en } }, defaultNS: "common", interpolation: { escapeValue: false },
  });
});

beforeEach(() => {
  for (const fn of Object.values(mp)) fn.mockClear();
  mp.list.mockImplementation(async () => pageBody());
  for (const fn of Object.values(roles)) fn.mockReset();
  roles.modelDefaults.mockImplementation(async () => rolesBody());
  roles.set.mockImplementation(async (role: string, modelId: string) => roleRow({ role, model_id: modelId, overridden_by: "ops" }));
  roles.reset.mockImplementation(async (role: string) => roleRow({ role }));
  resetModelDefaultsForTests();
});
afterEach(cleanup);

function auth(isAdmin: boolean): AuthContextValue {
  return {
    authRequired: true, username: isAdmin ? "ops" : "alice", userId: isAdmin ? null : "u-alice",
    role: isAdmin ? "admin" : "member", email: null,
    accountExpiresAt: null, isAdmin, can: () => isAdmin, logout: async () => undefined,
  };
}

const renderAt = (isAdmin: boolean, search = "") =>
  render(
    <AuthContext.Provider value={auth(isAdmin)}>
      <MemoryRouter initialEntries={[`/v2/settings/model-providers${search}`]}>
        <V2ModelProviders />
      </MemoryRouter>
    </AuthContext.Provider>,
  );

describe("owner-bound providers (N17-obs203)", () => {
  it("is the last entry of the V2 Configuration group, open to every member", () => {
    // user decision 2026-10-21 (N17-obs224 state update 2): the entry lives in the
    // Configuration group, not the platform-services group
    const admin = V2_NAV.find((g) => g.key === "admin");
    const entry = admin?.items.find((i) => i.to === "/v2/settings/model-providers");
    expect(entry).toBeTruthy();
    expect(admin?.items[admin.items.length - 1]).toBe(entry); // after workspaces / users
    expect(entry?.admin).toBeUndefined(); // N17-obs203: members register their own providers
    expect(V2_NAV.find((g) => g.key === "run")?.items.some((i) => i.to === "/v2/settings/model-providers")).toBe(false);
    expect(entry?.labelKey).toBe("nav.modelProviders");
    expect(entry?.feature).toBeUndefined(); // the registry always exists — no feature flag
  });

  it("a member sees the providers view only: their own card is editable, the others read-only", async () => {
    mp.list.mockImplementation(async () => ({ ...pageBody(), providers: [bedrock, siliconflow, bobShared, ppio], workspaces: [] }));
    renderAt(false);
    expect(await screen.findByTestId("v2-mp-provider-ppio")).toBeTruthy();
    expect(mp.list).toHaveBeenCalledTimes(1);
    // N17-obs251: a member gets the providers + roles (read-only) tabs, not workspaces / changes
    expect(screen.getAllByRole("tab").map((el) => el.textContent)).toEqual([en.modelProviders.view.providers, en.modelProviders.view.roles]);
    expect(screen.queryByText(en.modelProviders.view.changes)).toBeNull();
    expect(screen.queryByText(en.modelProviders.view.workspaces)).toBeNull();
    expect(screen.getByText(en.modelProviders.memberMeta)).toBeTruthy();
    expect(roles.modelDefaults).not.toHaveBeenCalled(); // the roles load on their tab only
    // scope badges: personal · mine for alice's own row, workspace for the shared ones
    expect(screen.getByTestId("v2-mp-scope-ppio").textContent).toBe(`${en.modelProviders.scope.personal} · ${en.modelProviders.mine}`);
    expect(screen.getByTestId("v2-mp-scope-bobvendor").textContent).toBe(en.modelProviders.scope.workspace);
    expect(screen.getByTestId("v2-mp-creator-bobvendor").textContent).toBe("bob");
    // alice may edit / delete / probe her own provider …
    expect(screen.getByTestId("v2-mp-edit-ppio")).toBeTruthy();
    expect(screen.getByTestId("v2-mp-delete-ppio")).toBeTruthy();
    expect(screen.getByTestId("v2-mp-probe-ppio")).toBeTruthy();
    // … but not bob's shared one nor the platform's (NULL creator = an administrator's)
    for (const name of ["bobvendor", "bedrock", "siliconflow"]) {
      expect(screen.queryByTestId(`v2-mp-edit-${name}`)).toBeNull();
      expect(screen.queryByTestId(`v2-mp-delete-${name}`)).toBeNull();
      expect(screen.queryByTestId(`v2-mp-probe-${name}`)).toBeNull();
      expect(screen.getByTestId(`v2-mp-readonly-${name}`).textContent).toBe(en.v2.common.creatorOnly);
      expect((screen.getByTestId(`v2-mp-provider-toggle-${name}`) as HTMLButtonElement).disabled).toBe(true);
    }
    // the selected (first) provider is bedrock — its model table is read-only for alice
    await screen.findByTestId("v2-mp-models");
    expect(screen.queryByTestId("v2-mp-add-model")).toBeNull();
    expect(screen.queryByTestId("v2-mp-model-edit-global.zai.glm-5.3")).toBeNull();
    expect((screen.getByTestId("v2-mp-model-toggle-global.zai.glm-5.3") as HTMLButtonElement).disabled).toBe(true);
  });

  it("a member's add drawer has no scope picker, a derived SSM name, and posts without scope", async () => {
    renderAt(false);
    await screen.findByTestId("v2-mp-provider-siliconflow");
    fireEvent.click(screen.getByTestId("v2-mp-add-provider"));
    const drawer = await screen.findByTestId("v2-mp-provider-drawer");
    expect(within(drawer).queryByTestId("v2-mp-provider-scope")).toBeNull();
    const ssm = within(drawer).getByTestId("v2-mp-provider-ssm") as HTMLInputElement;
    expect(ssm.disabled).toBe(true);
    expect(within(drawer).getByText(en.modelProviders.hint.ssmDerived)).toBeTruthy();
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-name"), { target: { value: "ppio" } });
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-label"), { target: { value: "PPIO" } });
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-url"), { target: { value: "https://api.ppinfra.com/v3/openai" } });
    fireEvent.click(within(drawer).getByTestId("v2-mp-provider-save"));
    await waitFor(() =>
      expect(mp.create).toHaveBeenCalledWith({
        name: "ppio", kind: "openai_compatible", label: "PPIO", base_url: "https://api.ppinfra.com/v3/openai",
        model_region: null, credential_ssm_parameter: null, enabled: true,
      }),
    );
    expect("scope" in (mp.create.mock.calls[0][0] as object)).toBe(false);
  });

  it("a non-admin creator of a shared row edits label / enabled only: endpoint, key and SSM name are locked (N17-obs225)", async () => {
    // a legacy re-pointed workspace-scope row alice still "owns" (created_by = u-alice)
    const aliceShared: ModelProviderOut = { ...ppio, id: "mp_legacy", name: "legacyvendor", label: "Legacy vendor", scope: "workspace", id_prefix: "legacyvendor." };
    mp.list.mockImplementation(async () => ({ ...pageBody(), providers: [bedrock, aliceShared], workspaces: [] }));
    renderAt(false);
    await screen.findByTestId("v2-mp-provider-legacyvendor");
    fireEvent.click(screen.getByTestId("v2-mp-edit-legacyvendor"));
    const drawer = await screen.findByTestId("v2-mp-provider-drawer");
    expect((within(drawer).getByTestId("v2-mp-provider-url") as HTMLInputElement).disabled).toBe(true);
    expect(within(drawer).queryByTestId("v2-mp-provider-api-key")).toBeNull(); // no key entry on a shared row
    expect((within(drawer).getByTestId("v2-mp-provider-ssm") as HTMLInputElement).disabled).toBe(true);
    expect(within(drawer).getByText(en.modelProviders.hint.sharedCredentialLocked)).toBeTruthy();
    // label stays editable and the save still goes through the typed client
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-label"), { target: { value: "Legacy vendor CN" } });
    fireEvent.click(within(drawer).getByTestId("v2-mp-provider-save"));
    await waitFor(() => expect(mp.update).toHaveBeenCalled());
    expect((mp.update.mock.calls[0][1] as { label?: string; api_key?: string }).label).toBe("Legacy vendor CN");
    expect("api_key" in (mp.update.mock.calls[0][1] as object)).toBe(false);
  });

  it("an administrator's drawer picks the scope and may share a member's provider", async () => {
    mp.list.mockImplementation(async () => ({ ...pageBody(), providers: [bedrock, siliconflow, ppio] }));
    renderAt(true);
    await screen.findByTestId("v2-mp-provider-ppio");
    // the administrator may edit every row, the member's included
    fireEvent.click(screen.getByTestId("v2-mp-edit-ppio"));
    const drawer = await screen.findByTestId("v2-mp-provider-drawer");
    const scope = within(drawer).getByTestId("v2-mp-provider-scope");
    expect(scope.textContent).toContain(en.modelProviders.scope.personal);
    fireEvent.click(scope);
    fireEvent.click(screen.getAllByRole("option").find((o) => o.textContent === en.modelProviders.scope.workspace)!);
    fireEvent.click(within(drawer).getByTestId("v2-mp-provider-save"));
    await waitFor(() => expect(mp.update).toHaveBeenCalled());
    expect(mp.update.mock.calls[0][0]).toBe("mp_ppio");
    expect((mp.update.mock.calls[0][1] as { scope?: string }).scope).toBe("workspace");
  });
});

describe("provider list + model table", () => {
  it("renders kind, endpoint host, credential reference status and the warnings", async () => {
    renderAt(true);
    expect(await screen.findByTestId("v2-mp-provider-siliconflow")).toBeTruthy();
    expect(mp.list).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("v2-mp-kind-siliconflow").textContent).toBe(en.modelProviders.kind.openai_compatible);
    expect(screen.getByTestId("v2-mp-kind-bedrock").textContent).toBe(en.modelProviders.kind.bedrock);
    expect(screen.getByText("api.siliconflow.cn")).toBeTruthy();
    // the credential is a NAME (SSM parameter) — never a value
    expect(screen.getByTestId("v2-mp-cred-siliconflow").textContent).toContain("launchpad-siliconflow-api-key");
    expect(screen.getByTestId("v2-mp-cred-bedrock").textContent).toContain("LAUNCHPAD_GLOBAL_BEDROCK_API_KEY");
    expect(document.body.textContent).not.toMatch(/sk-|Bearer /);
    expect(screen.getByTestId("v2-mp-warnings").textContent).toBe(en.modelProviders.warning.probe_failed);
    // built-in rows cannot be deleted; others can
    expect(screen.queryByTestId("v2-mp-delete-bedrock")).toBeNull();
    expect(screen.getByTestId("v2-mp-delete-siliconflow")).toBeTruthy();
    // Register credential only for an SSM-backed provider that is not configured yet
    expect(screen.getByTestId("v2-mp-credential-siliconflow").textContent).toBe(en.modelProviders.action.registerCredential);
    expect(screen.queryByTestId("v2-mp-credential-bedrock")).toBeNull();
  });

  it("shows the first provider's models with capability chips and source badges", async () => {
    renderAt(true);
    await screen.findByTestId("v2-mp-models");
    const table = screen.getByTestId("v2-mp-models");
    expect(within(table).getAllByRole("row")).toHaveLength(4); // header + glm, kimi, titan
    expect(screen.getByTestId("v2-mp-cap-global.moonshotai.kimi-k3-tools").textContent).toBe(`${en.modelProviders.cap.tools} ✓`);
    expect(screen.getByTestId("v2-mp-cap-global.moonshotai.kimi-k3-temperature").textContent).toBe(`${en.modelProviders.cap.temperature} ✗`);
    expect(screen.getByTestId("v2-mp-cap-global.zai.glm-5.3-max_output_tokens").textContent).toBe(`${en.modelProviders.capShort.max_output_tokens} 16,384`);
    expect(screen.getByTestId("v2-mp-cap-global.zai.glm-5.3-default_judge_max_tokens").textContent).toBe(`${en.modelProviders.capShort.default_judge_max_tokens} 4,096`);
    // an embedding row shows only its dimension
    expect(screen.getByTestId("v2-mp-cap-amazon.titan-embed-text-v2:0-embedding_dimension").textContent).toBe(`${en.modelProviders.capShort.embedding_dimension} 1,024`);
    expect(screen.queryByTestId("v2-mp-cap-amazon.titan-embed-text-v2:0-tools")).toBeNull();
    expect(screen.getByTestId("v2-mp-source-global.zai.glm-5.3").textContent).toBe(en.modelProviders.source.builtin);
    expect(screen.getByTestId("v2-mp-source-global.moonshotai.kimi-k3").textContent).toBe(en.modelProviders.source.probed);
    // the per-field tooltip names the provenance (vision was set by hand)
    expect(screen.getByTestId("v2-mp-cap-global.moonshotai.kimi-k3-vision").getAttribute("title")).toContain(en.modelProviders.source.manual);
  });

  it("selects a provider from ?provider= and flags an unverified field", async () => {
    renderAt(true, "?provider=mp_sf");
    await screen.findByTestId("v2-mp-models");
    expect(screen.getByTestId("v2-mp-source-siliconflow.Qwen/Qwen3-8B").textContent).toBe(en.modelProviders.source.manual);
    expect(screen.getByTestId("v2-mp-cap-siliconflow.Qwen/Qwen3-8B-temperature").textContent).toBe(`${en.modelProviders.cap.temperature} ?`);
    expect(screen.getByText(en.modelProviders.source.unknown)).toBeTruthy();
  });
});

describe("writes go through the typed client", () => {
  it("enable toggle of a model → PUT {enabled:false} and a reload", async () => {
    renderAt(true);
    await screen.findByTestId("v2-mp-models");
    fireEvent.click(screen.getByTestId("v2-mp-model-toggle-global.moonshotai.kimi-k3"));
    await waitFor(() => expect(mp.updateModel).toHaveBeenCalledWith("mp_bedrock", "md_kimi", { enabled: false }));
    await waitFor(() => expect(mp.list).toHaveBeenCalledTimes(2));
  });

  // D21-obs8: `PUT /{id}` is `ProviderUpdate` (extra keys forbidden) — the body must carry
  // exactly these keys; `name` / `kind` made every toggle answer 422 "Request validation failed".
  const PROVIDER_UPDATE_KEYS = ["label", "base_url", "model_region", "credential_ssm_parameter", "enabled"];

  it("disabling a provider asks first, then PUTs the ProviderUpdate keys (no name/kind) with enabled:false", async () => {
    renderAt(true);
    await screen.findByTestId("v2-mp-provider-siliconflow");
    fireEvent.click(screen.getByTestId("v2-mp-provider-toggle-siliconflow"));
    fireEvent.click(await screen.findByTestId("v2-confirm-ok"));
    await waitFor(() =>
      expect(mp.update).toHaveBeenCalledWith("mp_sf", {
        label: "SiliconFlow", base_url: "https://api.siliconflow.cn/v1",
        model_region: null, credential_ssm_parameter: "launchpad-siliconflow-api-key", enabled: false,
      }),
    );
    expect(Object.keys(mp.update.mock.calls[0][1]).sort()).toEqual([...PROVIDER_UPDATE_KEYS].sort());
  });

  it("enabling a disabled provider PUTs the ProviderUpdate keys (no name/kind) with enabled:true, no confirm", async () => {
    mp.list.mockResolvedValueOnce({ ...pageBody(), providers: [bedrock, { ...siliconflow, enabled: false }] });
    renderAt(true);
    await screen.findByTestId("v2-mp-provider-siliconflow");
    fireEvent.click(screen.getByTestId("v2-mp-provider-toggle-siliconflow"));
    await waitFor(() =>
      expect(mp.update).toHaveBeenCalledWith("mp_sf", {
        label: "SiliconFlow", base_url: "https://api.siliconflow.cn/v1",
        model_region: null, credential_ssm_parameter: "launchpad-siliconflow-api-key", enabled: true,
      }),
    );
    expect(screen.queryByTestId("v2-confirm-ok")).toBeNull();
    expect(Object.keys(mp.update.mock.calls[0][1])).not.toContain("name");
    expect(Object.keys(mp.update.mock.calls[0][1])).not.toContain("kind");
  });

  it("edit drawer PUTs the ProviderUpdate keys only — name and kind stay out of the update body", async () => {
    renderAt(true);
    await screen.findByTestId("v2-mp-provider-siliconflow");
    fireEvent.click(screen.getByTestId("v2-mp-edit-siliconflow"));
    const drawer = await screen.findByTestId("v2-mp-provider-drawer");
    expect((within(drawer).getByTestId("v2-mp-provider-name") as HTMLInputElement).disabled).toBe(true);
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-label"), { target: { value: "SiliconFlow CN" } });
    fireEvent.click(within(drawer).getByTestId("v2-mp-provider-save"));
    await waitFor(() =>
      expect(mp.update).toHaveBeenCalledWith("mp_sf", {
        label: "SiliconFlow CN", base_url: "https://api.siliconflow.cn/v1",
        model_region: null, credential_ssm_parameter: "launchpad-siliconflow-api-key", enabled: true,
        scope: "workspace", // N17-obs203: an administrator's body carries the scope
      }),
    );
    expect(Object.keys(mp.update.mock.calls[0][1]).sort()).toEqual([...PROVIDER_UPDATE_KEYS, "scope"].sort());
    expect(mp.create).not.toHaveBeenCalled();
  });

  it("Register credential posts to /credential with no key material", async () => {
    renderAt(true);
    await screen.findByTestId("v2-mp-provider-siliconflow");
    fireEvent.click(screen.getByTestId("v2-mp-credential-siliconflow"));
    await waitFor(() => expect(mp.credential).toHaveBeenCalledWith("mp_sf"));
    expect(mp.credential.mock.calls[0]).toHaveLength(1);
  });

  it("edit drawer: only the changed capability is sent (it becomes manual) and source badges show", async () => {
    renderAt(true);
    await screen.findByTestId("v2-mp-models");
    fireEvent.click(screen.getByTestId("v2-mp-model-edit-global.moonshotai.kimi-k3"));
    const drawer = await screen.findByTestId("v2-mp-model-drawer");
    expect(within(drawer).getAllByText(en.modelProviders.source.probed).length).toBeGreaterThan(0);
    fireEvent.click(within(drawer).getByTestId("v2-mp-model-temperature")); // false → true
    fireEvent.change(within(drawer).getByTestId("v2-mp-model-judge"), { target: { value: "2048" } });
    fireEvent.click(within(drawer).getByTestId("v2-mp-model-save"));
    await waitFor(() =>
      expect(mp.updateModel).toHaveBeenCalledWith("mp_bedrock", "md_kimi", {
        inference_params: { temperature: true, top_p: true },
        default_judge_max_tokens: 2048,
      }),
    );
    await waitFor(() => expect(screen.queryByTestId("v2-mp-model-drawer")).toBeNull());
  });

  it("add model drawer: probe on add is the default and the overrides stay minimal", async () => {
    renderAt(true, "?provider=mp_sf");
    await screen.findByTestId("v2-mp-models");
    fireEvent.click(screen.getByTestId("v2-mp-add-model"));
    const drawer = await screen.findByTestId("v2-mp-model-drawer");
    expect((within(drawer).getByTestId("v2-mp-model-probe-on-add") as HTMLInputElement).checked).toBe(true);
    expect(within(drawer).getByTestId("v2-mp-model-save").textContent).toBe(en.modelProviders.action.probeAndSave);
    // the datalist offers the ids the last provider probe listed
    expect(drawer.querySelectorAll("datalist option")).toHaveLength(2);
    fireEvent.change(within(drawer).getByTestId("v2-mp-model-name"), { target: { value: "Qwen/Qwen3-8B" } });
    fireEvent.click(within(drawer).getByTestId("v2-mp-model-save"));
    await waitFor(() => expect(mp.addModel).toHaveBeenCalledWith("mp_sf", { api_model_name: "Qwen/Qwen3-8B", kind: "chat", probe: true }));
  });

  it("add provider drawer validates https + name pattern + SSM pattern before posting", async () => {
    renderAt(true);
    await screen.findByTestId("v2-mp-provider-siliconflow");
    fireEvent.click(screen.getByTestId("v2-mp-add-provider"));
    const drawer = await screen.findByTestId("v2-mp-provider-drawer");
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-name"), { target: { value: "Bad_Name" } });
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-label"), { target: { value: "Vendor" } });
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-url"), { target: { value: "http://10.0.0.1/v1" } });
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-ssm"), { target: { value: "my-secret" } });
    fireEvent.click(within(drawer).getByTestId("v2-mp-provider-save"));
    expect(mp.create).not.toHaveBeenCalled();
    expect(within(drawer).getByText(en.modelProviders.validation.namePattern)).toBeTruthy();
    expect(within(drawer).getByText(en.modelProviders.validation.url.https)).toBeTruthy();
    expect(within(drawer).getByText(en.modelProviders.validation.ssmPattern)).toBeTruthy();
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-name"), { target: { value: "vendor" } });
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-url"), { target: { value: "https://api.vendor.example/v1" } });
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-ssm"), { target: { value: "launchpad-vendor-api-key" } });
    fireEvent.click(within(drawer).getByTestId("v2-mp-provider-save"));
    await waitFor(() =>
      expect(mp.create).toHaveBeenCalledWith({
        name: "vendor", kind: "openai_compatible", label: "Vendor", base_url: "https://api.vendor.example/v1",
        model_region: null, credential_ssm_parameter: "launchpad-vendor-api-key", enabled: true, scope: "workspace",
      }),
    );
  });

  it("a 409 in_use on a provider delete names the platform roles and system presets (N17-obs207)", async () => {
    mp.remove.mockImplementation(async () => {
      throw new ApiError("model_provider.in_use", "still referenced", {
        referrers: [{ kind: "agent", id: "ag_1", name: "eval-worker-preset" }, { kind: "setting", id: "eval_judge_model_id", name: "eval_judge_model_id", role: "judge" }],
        agents: [{ id: "ag_1", name: "eval-worker-preset", workspace_id: "default", system_key: "eval-worker" }],
        roles: [{ role: "judge", field: "eval_judge_model_id", model_ids: ["siliconflow.Qwen/Qwen3-8B"] }],
      });
    });
    renderAt(true);
    await screen.findByTestId("v2-mp-provider-siliconflow");
    expect(screen.getByTestId("v2-mp-delete-siliconflow")).toBeTruthy(); // not builtin → deletable
    expect(screen.queryByTestId("v2-mp-delete-bedrock")).toBeNull(); // the hub Bedrock row stays undeletable
    fireEvent.click(screen.getByTestId("v2-mp-delete-siliconflow"));
    fireEvent.click(await screen.findByTestId("v2-confirm-ok"));
    const dialog = await screen.findByTestId("v2-mp-in-use");
    expect(within(dialog).getByTestId("v2-mp-in-use-roles").textContent).toContain(en.modelProviders.roles.role.judge.label);
    expect(within(dialog).getByTestId("v2-mp-in-use-presets").textContent).toContain("eval-worker-preset");
    expect(within(dialog).getByTestId("v2-mp-referrers").textContent).toContain("eval-worker-preset");
  });

  it("a 409 in_use answer renders the referrer list with links instead of a toast", async () => {
    mp.removeModel.mockImplementationOnce(async () => {
      throw new ApiError("model.in_use", "in use", { referrers: [{ kind: "agent", id: "ag_1", name: "Front desk" }, { kind: "evaluator", id: "ev_9" }] });
    });
    renderAt(true, "?provider=mp_sf");
    await screen.findByTestId("v2-mp-models");
    fireEvent.click(screen.getByTestId("v2-mp-model-delete-siliconflow.Qwen/Qwen3-8B"));
    fireEvent.click(await screen.findByTestId("v2-confirm-ok"));
    const list = await screen.findByTestId("v2-mp-referrers");
    const links = within(list).getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual(["Front desk", "ev_9"]);
    expect(links[0].getAttribute("href")).toBe("/v2/agents?view=detail&id=ag_1");
    expect(links[1].getAttribute("href")).toContain("/v2/eval/evaluators");
  });
});

describe("probes (§7.4) and the M1-b gap", () => {
  it("a 501 probe_unavailable shows the not-available-yet hint", async () => {
    mp.probe.mockImplementationOnce(async () => {
      throw new ApiError("model_providers.probe_unavailable", "not yet", null);
    });
    renderAt(true);
    await screen.findByTestId("v2-mp-provider-siliconflow");
    fireEvent.click(screen.getByTestId("v2-mp-probe-siliconflow"));
    expect((await screen.findByTestId("v2-mp-probe-error")).textContent).toBe(en.modelProviders.probe.unavailable);
    expect(mp.probe).toHaveBeenCalledWith("mp_sf");
  });

  it("a provider probe lists the models seen and refreshes the cards", async () => {
    mp.probe.mockImplementationOnce(async () => ({ state: "ok", latency_ms: 310, models_seen: ["a/b", "c/d", "e/f"], error: null }));
    renderAt(true);
    await screen.findByTestId("v2-mp-provider-siliconflow");
    fireEvent.click(screen.getByTestId("v2-mp-probe-siliconflow"));
    const result = await screen.findByTestId("v2-mp-probe-result");
    expect(result.textContent).toContain(en.modelProviders.probeState.ok);
    expect(within(result).getAllByText(/^[ace]\/[bdf]$/)).toHaveLength(3);
    await waitFor(() => expect(mp.list).toHaveBeenCalledTimes(2));
  });

  it("a model re-probe is a dry run with a diff; Apply writes with apply:true", async () => {
    mp.probeModel.mockImplementation(async (_p, _m, apply) => ({
      findings: { exists: true, tools: true, temperature: true },
      diff: {
        temperature: { stored: false, probed: true, source: "probed" },
        vision: { stored: true, probed: false, source: "manual" },
      },
      applied: apply === true,
    }));
    renderAt(true);
    await screen.findByTestId("v2-mp-models");
    fireEvent.click(screen.getByTestId("v2-mp-model-probe-global.moonshotai.kimi-k3"));
    await screen.findByTestId("v2-mp-probe-diff");
    expect(mp.probeModel).toHaveBeenCalledWith("mp_bedrock", "md_kimi", false);
    expect(screen.getByTestId("v2-mp-diff-temperature").textContent).toContain("✗");
    expect(screen.getByTestId("v2-mp-diff-vision").textContent).toContain(en.modelProviders.source.manual);
    fireEvent.click(screen.getByTestId("v2-mp-probe-apply"));
    await waitFor(() => expect(mp.probeModel).toHaveBeenCalledWith("mp_bedrock", "md_kimi", true));
    await waitFor(() => expect(mp.list).toHaveBeenCalledTimes(2));
  });
});

describe("workspace flags and the change journal", () => {
  it("the matrix writes the workspace's disabled_models list", async () => {
    renderAt(true, "?view=workspaces");
    const matrix = await screen.findByTestId("v2-mp-matrix");
    expect((within(matrix).getByTestId("v2-mp-flag-t1-global.moonshotai.kimi-k3") as HTMLInputElement).checked).toBe(false);
    expect((within(matrix).getByTestId("v2-mp-flag-default-global.moonshotai.kimi-k3") as HTMLInputElement).checked).toBe(true);
    fireEvent.click(within(matrix).getByTestId("v2-mp-flag-default-global.zai.glm-5.3"));
    await waitFor(() => expect(mp.setWorkspaceFlags).toHaveBeenCalledWith("default", ["global.zai.glm-5.3"]));
    fireEvent.click(within(matrix).getByTestId("v2-mp-flag-t1-global.moonshotai.kimi-k3"));
    await waitFor(() => expect(mp.setWorkspaceFlags).toHaveBeenCalledWith("t1", []));
  });

  it("an administrator sees the selected provider's own audit trail under its models (N17-obs203 §4)", async () => {
    renderAt(true, "?provider=mp_sf");
    const card = await screen.findByTestId("v2-mp-provider-changes");
    await waitFor(() => expect(mp.changes).toHaveBeenCalledWith(10, undefined, "mp_sf"));
    expect(card.textContent).toContain(en.modelProviders.changes.providerTitle);
    expect(card.textContent).toContain("model.update");
  });

  it("a member never fires the journal request (administrators only)", async () => {
    renderAt(false);
    await screen.findByTestId("v2-mp-providers");
    await screen.findByTestId("v2-mp-models");
    expect(screen.queryByTestId("v2-mp-provider-changes")).toBeNull();
    expect(mp.changes).not.toHaveBeenCalled();
  });

  it("the journal lists operations newest first with a before→after summary", async () => {
    renderAt(true, "?view=changes");
    const table = await screen.findByTestId("v2-mp-changes-table");
    expect(mp.changes).toHaveBeenCalledWith(50, undefined, undefined);
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("v7");
    expect(rows[0].textContent).toContain("model.update");
    expect(screen.getByTestId("v2-mp-change-7").textContent).toContain("tools");
    expect(screen.getByTestId("v2-mp-change-7").textContent).toContain("false");
    expect(rows[1].textContent).toContain("seed");
  });
});

describe("model roles tab (r15-e, upstream #275 subset)", () => {
  /** a chat row without tool use on the SiliconFlow provider: a plain-Converse role may take it, a role that runs tools may not */
  const plain: ModelRowOut = {
    ...qwen, id: "md_plain", model_id: "siliconflow.deepseek-ai/Plain", api_model_name: "deepseek-ai/Plain",
    label: "Plain (no tools)", tools: false, sort_order: 4,
  };
  const optionIds = () => screen.getAllByRole("option").map((o) => o.textContent ?? "");

  beforeEach(() => {
    mp.list.mockImplementation(async () => ({ ...pageBody(), models: [glm, kimi, titan, qwen, plain] }));
    Element.prototype.scrollIntoView = () => undefined; // jsdom has none; the listbox scrolls its active option
  });

  it("is the second sub-tab and lists every role with its effective model, default and who overrode it", async () => {
    renderAt(true, "?view=roles");
    await screen.findByTestId("v2-mp-roles-table");
    expect(roles.modelDefaults).toHaveBeenCalledTimes(1);
    const tabs = screen.getAllByRole("tab").map((el) => el.textContent);
    expect(tabs).toEqual([en.modelProviders.view.providers, en.modelProviders.view.roles, en.modelProviders.view.workspaces, en.modelProviders.view.changes]);
    // N17-obs252: the Skill Lab roles (HIDDEN_SURFACES.features) and the Studio coding agent
    // (HIDDEN_SURFACES.createEntries.studio = false) are hidden with their features
    const visible = rolesBody().roles.filter((r) => !roleHidden(r.feature));
    expect(visible.map((r) => r.role)).toEqual(["agent_default", "judge", "judge_fallback", "actor", "prompt_opt", "memory_extract", "embedding"]);
    for (const r of visible) {
      expect(screen.getByTestId(`v2-mp-role-${r.role}`).textContent).toContain(en.modelProviders.roles.role[r.role as keyof typeof en.modelProviders.roles.role].label);
      expect(screen.getByTestId(`v2-mp-role-${r.role}`).textContent).toContain(r.field);
    }
    for (const hidden of ["skill_lab_judge", "skill_lab_target", "codegen"]) expect(screen.queryByTestId(`v2-mp-role-${hidden}`)).toBeNull();
    // the judge is overridden by ops (reset enabled); the agent default is built-in (reset disabled)
    expect(screen.getByTestId("v2-mp-role-status-judge").textContent).toContain("Overridden by ops");
    expect(screen.getByTestId("v2-mp-role-status-judge").textContent).toContain(`Default: ${GLM_ID}`);
    expect(screen.getByTestId("v2-mp-role-select-judge").textContent).toContain(KIMI_ID);
    expect((screen.getByTestId("v2-mp-role-reset-judge") as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByTestId("v2-mp-role-status-agent_default").textContent).toContain(en.modelProviders.roles.builtin);
    expect((screen.getByTestId("v2-mp-role-reset-agent_default") as HTMLButtonElement).disabled).toBe(true);
    // the judge fallback is "none" and may stay so
    expect(screen.getByTestId("v2-mp-role-select-judge_fallback").textContent).toBe(en.modelProviders.roles.noneOption);
    // nothing is dirty yet: every Save is disabled
    for (const r of visible) expect((screen.getByTestId(`v2-mp-role-save-${r.role}`) as HTMLButtonElement).disabled).toBe(true);
    // a role that runs tools is tagged
    expect(screen.getByTestId("v2-mp-role-agent_default").textContent).toContain(en.modelProviders.roles.needsTools);
    expect(screen.getByTestId("v2-mp-role-judge").textContent).not.toContain(en.modelProviders.roles.needsTools);
    // N17-obs202: one neutral line says the system presets read these roles at install / repair only
    expect(screen.getByTestId("v2-mp-roles-preset-hint").textContent).toBe(en.modelProviders.roles.presetHint);
    expect(zh.modelProviders.roles.presetHint).toContain("「新建 Agent → 系统预置」");
  });

  it("offers enabled chat rows only — tool-capable where the role runs tools — and saves a pick through PUT", async () => {
    // N17-obs203 §6: a member's personal provider's model is never a platform role's option
    const personalModel: ModelRowOut = { ...qwen, id: "md_ppio", provider_id: "mp_ppio", model_id: "ppio.deepseek/deepseek-v4", label: "DeepSeek (PPIO)", sort_order: 9 };
    mp.list.mockImplementation(async () => ({ ...pageBody(), providers: [bedrock, siliconflow, ppio], models: [glm, kimi, titan, qwen, plain, personalModel] }));
    renderAt(true, "?view=roles");
    await screen.findByTestId("v2-mp-roles-table");
    fireEvent.click(screen.getByTestId("v2-mp-role-select-agent_default"));
    let ids = optionIds();
    expect(ids.some((o) => o.includes("ppio.deepseek/deepseek-v4"))).toBe(false); // personal scope
    expect(ids.some((o) => o.includes(KIMI_ID))).toBe(true);
    expect(ids.some((o) => o.includes("siliconflow.Qwen/Qwen3-8B"))).toBe(true);
    expect(ids.some((o) => o.includes("siliconflow.deepseek-ai/Plain"))).toBe(false); // no tool use
    expect(ids.some((o) => o.includes("amazon.titan-embed-text-v2:0"))).toBe(false); // embedding
    fireEvent.click(screen.getAllByRole("option").find((o) => o.textContent?.includes(KIMI_ID))!);
    const save = screen.getByTestId("v2-mp-role-save-agent_default") as HTMLButtonElement;
    expect(save.disabled).toBe(false);
    fireEvent.click(save);
    await waitFor(() => expect(roles.set).toHaveBeenCalledWith("agent_default", KIMI_ID));
    await waitFor(() => expect(roles.modelDefaults).toHaveBeenCalledTimes(3)); // the tab's reload + the shared pickers' reload
    // the judge (plain Converse) may take the row without tools
    fireEvent.click(screen.getByTestId("v2-mp-role-select-judge"));
    ids = optionIds();
    expect(ids.some((o) => o.includes("siliconflow.deepseek-ai/Plain"))).toBe(true);
    expect(ids.some((o) => o.includes("amazon.titan-embed-text-v2:0"))).toBe(false);
  });

  it("resets an overridden role through DELETE and probes the picked model through the registry's re-probe", async () => {
    mp.probeModel.mockResolvedValue({ findings: { exists: true, tools: true }, diff: {}, errors: {}, raw_hint: {}, latency_ms: 5, applied: false });
    renderAt(true, "?view=roles");
    await screen.findByTestId("v2-mp-roles-table");
    fireEvent.click(screen.getByTestId("v2-mp-role-reset-judge"));
    await waitFor(() => expect(roles.reset).toHaveBeenCalledWith("judge"));
    // Probe = the registry's own re-probe of the row the role points at (no second probe path)
    // (the judge is overridden to Kimi in `rolesBody`; the Skill Lab rows are hidden now)
    fireEvent.click(screen.getByTestId("v2-mp-role-probe-judge"));
    await waitFor(() => expect(mp.probeModel).toHaveBeenCalledWith("mp_bedrock", "md_kimi", false));
    // a role whose value is no registry row cannot be probed
    expect((screen.getByTestId("v2-mp-role-probe-judge_fallback") as HTMLButtonElement).disabled).toBe(true);
  });

  it("surfaces a refused pick (422 models.model_not_allowed) and keeps the row", async () => {
    roles.set.mockRejectedValueOnce(new ApiError("models.model_not_allowed", "agent_default: 'x' does not support tool use", { role: "agent_default", model_id: KIMI_ID, reason: "no_tool_use" }));
    renderAt(true, "?view=roles");
    await screen.findByTestId("v2-mp-roles-table");
    fireEvent.click(screen.getByTestId("v2-mp-role-select-agent_default"));
    fireEvent.click(screen.getAllByRole("option").find((o) => o.textContent?.includes(KIMI_ID))!);
    fireEvent.click(screen.getByTestId("v2-mp-role-save-agent_default"));
    await waitFor(() => expect(roles.set).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId("v2-mp-role-agent_default")).toBeTruthy();
    expect(roles.modelDefaults).toHaveBeenCalledTimes(1); // nothing reloaded
  });

  // N17-obs251 (user ruling 2026-10-22): members see the roles tab READ-ONLY
  it("a member sees the roles read-only: no Save / Reset / Probe, pickers disabled, 'overridden by an administrator' without a name", async () => {
    roles.modelDefaults.mockImplementation(async () => ({
      roles: rolesBody().roles.map((r) => ({ ...r, overridden_by: r.overridden_by === null ? null : "" })), // the server redacts for members
    }));
    renderAt(false, "?view=roles");
    await screen.findByTestId("v2-mp-roles-table");
    expect(roles.modelDefaults).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("v2-mp-roles-readonly").textContent).toBe(en.modelProviders.roles.readOnly);
    expect(zh.modelProviders.roles.readOnly).toBe("仅管理员可修改模型角色。");
    expect(screen.getByTestId("v2-mp-role-judge").textContent).toContain(en.modelProviders.roles.role.judge.label);
    expect(screen.getByTestId("v2-mp-role-select-judge").textContent).toContain(KIMI_ID);
    expect(screen.getByTestId("v2-mp-role-status-judge").textContent).toContain(en.modelProviders.roles.overriddenByAdmin);
    expect(screen.getByTestId("v2-mp-role-status-judge").textContent).not.toContain("ops");
    expect(screen.getByTestId("v2-mp-role-status-agent_default").textContent).toContain(en.modelProviders.roles.builtin);
    for (const action of ["save", "reset", "probe"]) expect(screen.queryByTestId(`v2-mp-role-${action}-judge`)).toBeNull();
    expect(screen.queryByText(en.modelProviders.roles.col.actions)).toBeNull();
    // the obs252 rule holds for members too: hidden features hide their roles
    expect(screen.queryByTestId("v2-mp-role-skill_lab_target")).toBeNull();
    expect(screen.queryByTestId("v2-mp-role-codegen")).toBeNull();
    // the picker is disabled: a click opens no listbox
    fireEvent.click(screen.getByTestId("v2-mp-role-select-judge"));
    expect(screen.queryAllByRole("option")).toHaveLength(0);
    expect(roles.set).not.toHaveBeenCalled();
  });

  it("an administrator still sees the username of who overrode a role", async () => {
    renderAt(true, "?view=roles");
    await screen.findByTestId("v2-mp-roles-table");
    expect(screen.getByTestId("v2-mp-role-status-judge").textContent).toContain(en.modelProviders.roles.overriddenBy.replace("{{who}}", "ops"));
  });

  // N17-obs252: every platform model choice is a role — the embedding row included
  it("the embedding role offers embedding rows only and its status reads the active registry row", async () => {
    renderAt(true, "?view=roles");
    await screen.findByTestId("v2-mp-roles-table");
    expect(screen.getByTestId("v2-mp-role-memory_extract").textContent).toContain(en.modelProviders.roles.role.memory_extract.label);
    expect(screen.getByTestId("v2-mp-role-embedding").textContent).toContain(en.modelProviders.roles.role.embedding.label);
    expect(screen.getByTestId("v2-mp-role-status-embedding").textContent).toContain(en.modelProviders.roles.builtin);
    fireEvent.click(screen.getByTestId("v2-mp-role-select-embedding"));
    const ids = optionIds();
    expect(ids.some((o) => o.includes("amazon.titan-embed-text-v2:0"))).toBe(true);
    expect(ids.some((o) => o.includes(GLM_ID) || o.includes(KIMI_ID))).toBe(false); // chat rows never
  });

  it("an embedding change refused by the reindex guard (409) says what to do; an allowed one saves through the roles API", async () => {
    const titan1: ModelRowOut = { ...titan, id: "md_titan1", model_id: "amazon.titan-embed-text-v1", api_model_name: "amazon.titan-embed-text-v1", label: "Titan v1", is_default: false, embedding_dimension: 768, sort_order: 3 };
    mp.list.mockImplementation(async () => ({ ...pageBody(), models: [glm, kimi, titan, titan1, qwen, plain] }));
    const toasts: [string, string][] = [];
    render(
      <AuthContext.Provider value={auth(true)}>
        <ToastContext.Provider value={(tone, text) => toasts.push([tone, text])}>
          <MemoryRouter initialEntries={["/v2/settings/model-providers?view=roles"]}>
            <V2ModelProviders />
          </MemoryRouter>
        </ToastContext.Provider>
      </AuthContext.Provider>,
    );
    await screen.findByTestId("v2-mp-roles-table");
    fireEvent.click(screen.getByTestId("v2-mp-role-select-embedding"));
    fireEvent.click(screen.getAllByRole("option").find((o) => o.textContent?.includes("amazon.titan-embed-text-v1"))!);
    roles.set.mockRejectedValueOnce(new ApiError("embedding.reindex_required", "the embedding model cannot change while vector indices hold documents", { indices: [{ name: "launchpad-memory", docs: 3 }] }));
    fireEvent.click(screen.getByTestId("v2-mp-role-save-embedding"));
    await waitFor(() => expect(roles.set).toHaveBeenCalledWith("embedding", "amazon.titan-embed-text-v1"));
    await waitFor(() => expect(toasts).toContainEqual(["error", en.modelProviders.roles.embeddingReindex]));
    expect(zh.modelProviders.roles.embeddingReindex).toBe("已有知识库/记忆索引含文档，更换向量模型需先重建索引。");
    // an empty substrate: the same save goes through and reloads the tab
    roles.set.mockImplementationOnce(async () => roleRow({ role: "embedding", field: "", model_id: "amazon.titan-embed-text-v1", default: "amazon.titan-embed-text-v2:0", kind: "embedding", overridden_by: "ops" }));
    fireEvent.click(screen.getByTestId("v2-mp-role-save-embedding"));
    await waitFor(() => expect(roles.set).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(toasts.some(([tone]) => tone === "success")).toBe(true));
    expect(roles.modelDefaults.mock.calls.length).toBeGreaterThan(1); // the tab (and the shared defaults) reloaded
  });
});

describe("API key typed in the console (N17-obs203 §3)", () => {
  const SECRET = "sk-typed-0123456789-SECRET-wxyz";

  it("the add drawer sends the typed key once (write-only) and clears it", async () => {
    renderAt(true);
    await screen.findByTestId("v2-mp-provider-siliconflow");
    fireEvent.click(screen.getByTestId("v2-mp-add-provider"));
    const drawer = await screen.findByTestId("v2-mp-provider-drawer");
    const keyInput = within(drawer).getByTestId("v2-mp-provider-api-key") as HTMLInputElement;
    expect(keyInput.type).toBe("password");
    expect(keyInput.autocomplete).toBe("off");
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-name"), { target: { value: "ppio" } });
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-label"), { target: { value: "PPIO" } });
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-url"), { target: { value: "https://api.ppinfra.com/v3/openai" } });
    fireEvent.change(keyInput, { target: { value: SECRET } });
    fireEvent.click(within(drawer).getByTestId("v2-mp-provider-save"));
    await waitFor(() => expect(mp.create).toHaveBeenCalled());
    expect((mp.create.mock.calls[0][0] as { api_key?: string }).api_key).toBe(SECRET);
    // cleared from the form before the request returns; never rendered anywhere
    expect(keyInput.value).toBe("");
    expect(document.body.innerHTML).not.toContain(SECRET);
  });

  it("the API key is the primary credential input; the SSM name sits under a collapsed Advanced section", async () => {
    renderAt(true);
    await screen.findByTestId("v2-mp-provider-siliconflow");
    fireEvent.click(screen.getByTestId("v2-mp-add-provider"));
    const drawer = await screen.findByTestId("v2-mp-provider-drawer");
    const keyField = within(drawer).getByTestId("v2-mp-provider-api-key");
    const advanced = within(drawer).getByTestId("v2-mp-provider-advanced") as HTMLDetailsElement;
    expect(advanced.open).toBe(false); // collapsed by default
    expect(advanced.textContent).toContain(en.modelProviders.advanced);
    expect(within(advanced).getByTestId("v2-mp-provider-ssm")).toBeTruthy(); // the SSM name lives inside it
    // the key field comes BEFORE the advanced block in document order
    expect(keyField.compareDocumentPosition(advanced) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(drawer).getByText(en.modelProviders.hint.apiKey)).toBeTruthy();
  });

  it("the add drawer omits api_key when nothing was typed", async () => {
    renderAt(true);
    await screen.findByTestId("v2-mp-provider-siliconflow");
    fireEvent.click(screen.getByTestId("v2-mp-add-provider"));
    const drawer = await screen.findByTestId("v2-mp-provider-drawer");
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-name"), { target: { value: "ppio" } });
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-label"), { target: { value: "PPIO" } });
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-url"), { target: { value: "https://api.ppinfra.com/v3/openai" } });
    fireEvent.click(within(drawer).getByTestId("v2-mp-provider-save"));
    await waitFor(() => expect(mp.create).toHaveBeenCalled());
    expect("api_key" in (mp.create.mock.calls[0][0] as object)).toBe(false);
  });

  it("the card shows configured · ****last4 and Replace key posts the typed key to /credential", async () => {
    const configured: ModelProviderOut = { ...siliconflow, configured: true, key_version: 2, key_last4: "abcd" };
    mp.list.mockImplementation(async () => ({ ...pageBody(), providers: [bedrock, configured] }));
    mp.credential.mockImplementation(async () => ({ configured: true as const, created: false, rotated: true, workspaces_updated: 1, key_version: 3, last4: "wxyz" }));
    renderAt(true);
    await screen.findByTestId("v2-mp-provider-siliconflow");
    expect(screen.getByTestId("v2-mp-cred-siliconflow").textContent).toContain(en.modelProviders.credential.configuredLast4.replace("{{last4}}", "abcd"));
    expect(screen.getByTestId("v2-mp-replace-key-siliconflow").textContent).toBe(en.modelProviders.action.replaceKey);
    fireEvent.click(screen.getByTestId("v2-mp-replace-key-siliconflow"));
    const dialog = await screen.findByTestId("v2-mp-key-dialog");
    const input = within(dialog).getByTestId("v2-mp-key-input") as HTMLInputElement;
    expect(input.type).toBe("password");
    expect((within(dialog).getByTestId("v2-mp-key-save") as HTMLButtonElement).disabled).toBe(true); // nothing typed yet
    fireEvent.change(input, { target: { value: SECRET } });
    fireEvent.click(within(dialog).getByTestId("v2-mp-key-save"));
    await waitFor(() => expect(mp.credential).toHaveBeenCalledWith("mp_sf", { api_key: SECRET }));
    await waitFor(() => expect(screen.queryByTestId("v2-mp-key-dialog")).toBeNull());
    expect(mp.list).toHaveBeenCalledTimes(2); // the card reloads to show the new last4
    expect(document.body.innerHTML).not.toContain(SECRET);
  });

  it("changing the Base URL of a configured provider warns that the stored key is cleared unless a key is typed (N17-obs228)", async () => {
    const configured: ModelProviderOut = { ...siliconflow, configured: true, key_version: 2, key_last4: "abcd" };
    mp.list.mockImplementation(async () => ({ ...pageBody(), providers: [bedrock, configured] }));
    renderAt(true);
    await screen.findByTestId("v2-mp-provider-siliconflow");
    fireEvent.click(screen.getByTestId("v2-mp-edit-siliconflow"));
    const drawer = await screen.findByTestId("v2-mp-provider-drawer");
    expect(within(drawer).queryByTestId("v2-mp-provider-host-change-warning")).toBeNull();
    // a trailing-slash echo of the same host is not a change
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-url"), { target: { value: "https://api.siliconflow.cn/v1/" } });
    expect(within(drawer).queryByTestId("v2-mp-provider-host-change-warning")).toBeNull();
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-url"), { target: { value: "https://api.other-host.com/v1" } });
    expect(within(drawer).getByTestId("v2-mp-provider-host-change-warning").textContent).toBe(en.modelProviders.hint.hostChangeClearsKey);
    // typing a key in the same save removes the warning (the key is re-entered for the new host)
    fireEvent.change(within(drawer).getByTestId("v2-mp-provider-api-key"), { target: { value: SECRET } });
    expect(within(drawer).queryByTestId("v2-mp-provider-host-change-warning")).toBeNull();
    fireEvent.click(within(drawer).getByTestId("v2-mp-provider-save"));
    await waitFor(() => expect(mp.update).toHaveBeenCalled());
    const body = mp.update.mock.calls[0][1] as { base_url?: string; api_key?: string };
    expect(body.base_url).toBe("https://api.other-host.com/v1");
    expect(body.api_key).toBe(SECRET);
  });

  it("a provider without a typed key offers Enter key and keeps Register credential", async () => {
    renderAt(true);
    await screen.findByTestId("v2-mp-provider-siliconflow");
    expect(screen.getByTestId("v2-mp-replace-key-siliconflow").textContent).toBe(en.modelProviders.action.enterKey);
    expect(screen.getByTestId("v2-mp-credential-siliconflow").textContent).toBe(en.modelProviders.action.registerCredential);
    // a Bedrock (host-env) row has neither
    expect(screen.queryByTestId("v2-mp-replace-key-bedrock")).toBeNull();
  });
});

describe("catalog warnings (N17-obs150 / N17-obs235)", () => {
  const behind = (presetKey: string, installed: number, workspaceId = "default") => ({
    code: "preset_snapshot_behind",
    message: `system preset ${presetKey}: the installed snapshot states model catalog v${installed}, the current catalog is v7`,
    detail: { installed, current: 7, preset_key: presetKey, workspace_id: workspaceId, agent_id: `a-${presetKey}-${workspaceId}` },
  });
  // `pageBody()` lists workspace `default` under its own name
  // N17-obs208: every banner carries its action — "Open System presets" when the preset's
  // workspace is the current one (no WorkspaceContext in `renderAt` = unknown = current)
  const expectedText = (preset: string, workspace = "default") =>
    en.modelProviders.warning.preset_snapshot_behind.replace("{{preset}}", preset).replace("{{workspace}}", workspace) +
    en.modelProviders.warningAction.openPresets;

  it("names the preset in every preset_snapshot_behind banner — two presets behind are two distinct texts", async () => {
    mp.list.mockImplementation(async () => ({ ...pageBody(), warnings: [behind("aws-agent-solution-architect", 6), behind("eval-worker", 5)] }));
    renderAt(true);
    const banners = await screen.findByTestId("v2-mp-warnings");
    const architect = expectedText(en.modelProviders.presetName["aws-agent-solution-architect"]);
    const worker = expectedText(en.modelProviders.presetName["eval-worker"]);
    expect(architect).not.toBe(worker);
    expect(banners.textContent).toBe(`${architect}${worker}`);
    expect(banners.textContent).not.toContain("{{preset}}");
    expect(banners.textContent).not.toContain("architect preset");
  });

  it("falls back to the raw preset key for a preset the console has no display name for", async () => {
    mp.list.mockImplementation(async () => ({ ...pageBody(), warnings: [behind("future-preset", 1)] }));
    renderAt(true);
    const banners = await screen.findByTestId("v2-mp-warnings");
    expect(banners.textContent).toBe(expectedText("future-preset"));
  });

  it("names the workspace — the same preset behind in two workspaces is two distinct texts (N17-obs235)", async () => {
    mp.list.mockImplementation(async () => ({
      ...pageBody(),
      workspaces: [
        { id: "default", name: "Default hub", disabled_models: [] },
        { id: "ws-shy", name: "member1-test-workspace", disabled_models: [] },
      ],
      warnings: [behind("eval-worker", 10, "default"), behind("eval-worker", 10, "ws-shy")],
    }));
    renderAt(true);
    const banners = await screen.findByTestId("v2-mp-warnings");
    const worker = en.modelProviders.presetName["eval-worker"];
    const hub = expectedText(worker, "Default hub");
    const shy = expectedText(worker, "member1-test-workspace");
    expect(hub).not.toBe(shy);
    expect(banners.textContent).toBe(`${hub}${shy}`);
    expect(banners.textContent).not.toContain("{{workspace}}");
    expect(banners.textContent).not.toContain("ws-shy"); // the display name, not the id
  });

  // N17-obs208: the banner names the real path and leads there
  it("names New agent → System presets (no \"System agents\" page exists) and links the current workspace's banner to /agents/new", async () => {
    mp.list.mockImplementation(async () => ({ ...pageBody(), warnings: [behind("eval-worker", 5)] }));
    renderAt(true);
    const banners = await screen.findByTestId("v2-mp-warnings");
    expect(banners.textContent).not.toContain("System agents");
    expect(en.modelProviders.warning.preset_snapshot_behind).toContain("New agent → System presets");
    expect(en.modelProviders.warning.preset_snapshot_behind).toContain("Repair snapshot");
    expect(zh.modelProviders.warning.preset_snapshot_behind).toContain("「新建 Agent → 系统预置」点「修复快照」。");
    expect(zh.modelProviders.warning.preset_snapshot_behind).not.toContain("系统 Agent");
    const link = screen.getByTestId("v2-mp-warning-presets-eval-worker-default");
    expect(link.tagName).toBe("A");
    expect(link.getAttribute("href")).toBe("/agents/new");
    expect(link.textContent).toBe(en.modelProviders.warningAction.openPresets);
  });

  it("a preset behind in ANOTHER workspace switches the session there before opening the presets; an unselectable workspace is only named", async () => {
    mp.list.mockImplementation(async () => ({
      ...pageBody(),
      workspaces: [
        { id: "default", name: "Default hub", disabled_models: [] },
        { id: "ws-shy", name: "member1-test-workspace", disabled_models: [] },
        { id: "ws-far", name: "someone-else", disabled_models: [] },
      ],
      warnings: [behind("eval-worker", 10, "ws-shy"), behind("eval-worker", 10, "ws-far")],
    }));
    const select = vi.fn();
    const current = { id: "default", name: "Default hub", bootstrap_status: "ready", is_default: true } as unknown as Workspace;
    const shy = { id: "ws-shy", name: "member1-test-workspace", bootstrap_status: "ready", is_default: false } as unknown as Workspace;
    render(
      <AuthContext.Provider value={auth(true)}>
        <WorkspaceContext.Provider value={{ workspaces: [current, shy], current, allWorkspaces: true, select, refresh: async () => undefined }}>
          <MemoryRouter initialEntries={["/v2/settings/model-providers"]}>
            <Routes>
              <Route path="/v2/settings/model-providers" element={<V2ModelProviders />} />
              <Route path="/agents/new" element={<div data-testid="presets-page" />} />
            </Routes>
          </MemoryRouter>
        </WorkspaceContext.Provider>
      </AuthContext.Provider>,
    );
    await screen.findByTestId("v2-mp-warnings");
    const switcher = screen.getByTestId("v2-mp-warning-presets-eval-worker-ws-shy");
    expect(switcher.tagName).toBe("BUTTON");
    expect(switcher.textContent).toBe(en.modelProviders.warningAction.switchAndOpen.replace("{{workspace}}", "member1-test-workspace"));
    const far = screen.getByTestId("v2-mp-warning-presets-eval-worker-ws-far");
    expect(far.tagName).toBe("SPAN");
    expect(far.textContent).toBe(en.modelProviders.warningAction.switchFirst.replace("{{workspace}}", "someone-else"));
    fireEvent.click(switcher);
    expect(select).toHaveBeenCalledWith("ws-shy");
    await screen.findByTestId("presets-page");
  });

  it("falls back to the workspace id when neither the page nor the session knows the workspace", async () => {
    mp.list.mockImplementation(async () => ({ ...pageBody(), warnings: [behind("eval-worker", 10, "ws-unknown")] }));
    renderAt(true);
    const banners = await screen.findByTestId("v2-mp-warnings");
    expect(banners.textContent).toBe(expectedText(en.modelProviders.presetName["eval-worker"], "ws-unknown"));
  });
});

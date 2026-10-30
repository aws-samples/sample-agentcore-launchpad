// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { ApiError, type PolicyDetail, type PolicyEngine } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import { ToastContext } from "../../hooks";
import { PolicyEditor, TEMPORAL_EXAMPLE } from "./PolicyEditor";

/**
 * The editor of one engine's policy (Global structure): templates build Cedar, VALIDATE posts
 * `{statement}` to the engine's validate route, CREATE posts to the engine's policies route
 * (no scope / agent ids / dialect), PUBLISH is a plain confirmation with no workspace id, reason
 * or apply checkbox (N17-obs505 / N17-obs506), and the per-policy enforcement toggle
 * (Global `enforcementMode`) calls the engine-scoped route.
 */

vi.mock("../../../auth/auth-context", () => ({
  useAuth: () => ({ isAdmin: true, can: () => true }),
}));
vi.mock("../../../workspace/workspace-context", () => ({
  useWorkspace: () => ({ current: { id: "default", name: "default" } }),
}));

const ENGINE: PolicyEngine = {
  id: "pe_1", name: "hr_engine", description: null, policy_count: 1,
  attachments: [{ gateway_id: "gw_hr", gateway_name: "hr-gw", mode: "LOG_ONLY" }],
  created_at: "2026-10-28T09:00:00+08:00",
};
const validatePolicy = vi.fn<(...args: unknown[]) => Promise<object>>(async () => ({ ok: true, errors: [], warnings: [] }));
const policyEntities = vi.fn<(engineId: string, gatewayId?: string | null) => Promise<object>>(async () => ({ users: [], api_keys: [], agents: [], tools: [], templates: [] }));
const createPolicy = vi.fn(async (_engineId: string, input: { name: string }) => ({ id: "pol_new", name: input.name }));
const publishPolicy = vi.fn(async () => DETAIL);
const setPolicyEnforcementMode = vi.fn(async (_eid: string, _id: string, mode: string) => ({ ...DETAIL, enforcement_mode: mode }));
const STATEMENT = "forbid (principal, action, resource);";
const DETAIL: PolicyDetail = {
  id: "pol_x", engine_id: "pe_1", name: "forbid_payout", description: null, status: "active", enforcement_mode: "active",
  active_version: 1, draft_version: 2, created_by: "river", created_at: "2026-10-27T09:00:00+08:00",
  updated_at: "2026-10-27T09:00:00+08:00", statement: STATEMENT, validation: null,
  versions: [
    { id: "v1", policy_id: "pol_x", version: 1, statement: STATEMENT, validation: null, authored_by: "river",
      created_at: "2026-10-27T09:00:00+08:00", published_at: "2026-10-27T09:01:00+08:00" },
    { id: "v2", policy_id: "pol_x", version: 2, statement: "permit (principal, action, resource);", validation: null, authored_by: "river",
      created_at: "2026-10-27T10:00:00+08:00", published_at: null },
  ],
  kind: "stateless",
};
let detail: PolicyDetail = DETAIL;
vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getPolicyEngine: async () => ENGINE,
      policyEntities: (...args: unknown[]) => policyEntities(...(args as [string, string | null | undefined])),
      validatePolicy: (...args: unknown[]) => validatePolicy(...args),
      createPolicy: (engineId: string, input: { name: string }) => createPolicy(engineId, input),
      getPolicy: async () => detail,
      publishPolicy: (...args: unknown[]) => publishPolicy(...(args as [])),
      setPolicyEnforcementMode: (eid: string, id: string, mode: string) => setPolicyEnforcementMode(eid, id, mode),
    },
  };
});

beforeAll(async () => {
  Element.prototype.scrollIntoView = () => undefined;
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en } }, defaultNS: "common", interpolation: { escapeValue: false },
  });
});
afterEach(() => {
  cleanup();
  validatePolicy.mockClear();
  createPolicy.mockClear();
  publishPolicy.mockClear();
  setPolicyEnforcementMode.mockClear();
  detail = DETAIL;
});

const mount = (policyId: string | null = null) =>
  render(
    <ToastContext.Provider value={() => undefined}>
      <MemoryRouter>
        <PolicyEditor engineId="pe_1" policyId={policyId} />
      </MemoryRouter>
    </ToastContext.Provider>,
  );

describe("PolicyEditor — templates and the Cedar statement", () => {
  it("offers the two call_tool baselines (never an invoke one) and builds the Stage-A permit for the current workspace", async () => {
    mount();
    expect(await screen.findByTestId("v2-policy-prefill-permit_workspace_agents_call_tools")).toBeTruthy();
    expect(screen.getByTestId("v2-policy-prefill-permit_users_call_tools")).toBeTruthy();
    expect(screen.queryByTestId("v2-policy-prefill-permit_members_invoke")).toBeNull();
    // the tool catalog is the engine's, scoped to its first attached gateway
    expect(policyEntities).toHaveBeenCalledWith("pe_1", "gw_hr");
    fireEvent.click(screen.getByTestId("v2-policy-prefill-permit_workspace_agents_call_tools"));
    fireEvent.click(screen.getByTestId("v2-policy-build-draft"));
    const cedar = (screen.getByTestId("v2-policy-cedar") as HTMLTextAreaElement).value;
    expect(cedar).toContain('action == Launchpad::Action::"call_tool"');
    expect(cedar).not.toContain("invoke");
    fireEvent.click(screen.getByTestId("v2-policy-prefill-permit_users_call_tools"));
    fireEvent.click(screen.getByTestId("v2-policy-build-draft"));
    expect((screen.getByTestId("v2-policy-cedar") as HTMLTextAreaElement).value.match(/permit\(/g)).toHaveLength(4);
  });

  it("has no scope / agent / dialect controls; validates with {statement} on the engine route and creates on the engine", async () => {
    mount();
    await screen.findByTestId("v2-policy-cedar");
    for (const id of ["v2-policy-scope-workspace", "v2-policy-scope-agents", "v2-policy-dialect"]) expect(screen.queryByTestId(id)).toBeNull();
    fireEvent.change(screen.getByTestId("v2-policy-name"), { target: { value: "payout_cap" } });
    fireEvent.click(screen.getByTestId("v2-policy-temporal-example"));
    expect((screen.getByTestId("v2-policy-cedar") as HTMLTextAreaElement).value).toBe(TEMPORAL_EXAMPLE);
    expect(TEMPORAL_EXAMPLE).toContain('AgentCore::Action::"hr-database___create_payout"');
    expect(screen.getByTestId("v2-policy-temporal-note").textContent).toBe(en.v2.governance.policy.dialect.temporalNote);
    expect(screen.getByTestId("v2-policy-format")).toBeTruthy();
    fireEvent.click(screen.getByTestId("v2-policy-validate"));
    await waitFor(() => expect(validatePolicy).toHaveBeenCalledWith("pe_1", TEMPORAL_EXAMPLE, undefined));
    fireEvent.click(screen.getByTestId("v2-policy-save"));
    fireEvent.click(screen.getByTestId("v2-policy-confirm-ok"));
    await waitFor(() => expect(createPolicy).toHaveBeenCalledWith("pe_1", { name: "payout_cap", statement: TEMPORAL_EXAMPLE, description: null, prefill: null }));
    const body = createPolicy.mock.calls[0][1] as Record<string, unknown>;
    expect(body).not.toHaveProperty("scope");
    expect(body).not.toHaveProperty("agent_ids");
    expect(body).not.toHaveProperty("dialect");
  });
});

describe("PolicyEditor — publish without confirmation fields (N17-obs505 / obs506)", () => {
  it("preflights an existing draft as a replacement and identifies Gateway compilation", async () => {
    validatePolicy.mockResolvedValueOnce({ ok: true, errors: [], warnings: [], scope: "gateway_active_set" });
    mount("pol_x");
    fireEvent.click(await screen.findByTestId("v2-policy-validate"));
    await waitFor(() => expect(validatePolicy).toHaveBeenCalledWith("pe_1", DETAIL.versions[1].statement, "pol_x"));
    expect(await screen.findByText(en.v2.governance.policy.validPublishPreflight)).toBeTruthy();
  });

  it("keeps concrete compiler errors visible after publishing fails", async () => {
    publishPolicy.mockRejectedValueOnce(new ApiError("policy.invalid_statement", "ACTIVE set does not compile", {
      errors: [
        { policy_id: "pol_x", message: "attribute `name` on entity type `AgentCore::Gateway` not found" },
        { policy_id: "pol_x", message: "unexpected type: expected Bool but saw AgentCore::Gateway" },
      ],
    }));
    mount("pol_x");
    fireEvent.click(await screen.findByTestId("v2-policy-publish"));
    fireEvent.click(screen.getByTestId("v2-policy-confirm-ok"));
    expect(await screen.findByText("attribute `name` on entity type `AgentCore::Gateway` not found")).toBeTruthy();
    expect(screen.getByText("unexpected type: expected Bool but saw AgentCore::Gateway")).toBeTruthy();
    expect(screen.queryByTestId("v2-policy-confirm")).toBeNull();
  });

  it("publishes the draft after a plain confirm: no workspace id, no reason, no apply checkbox", async () => {
    mount("pol_x");
    fireEvent.click(await screen.findByTestId("v2-policy-publish"));
    const dialog = screen.getByTestId("v2-policy-confirm");
    expect(screen.getByTestId("v2-policy-confirm-body").textContent).toBe(
      en.v2.governance.policy.confirmBody.publish.replace("{{name}}", "forbid_payout").replace("{{version}}", "2"),
    );
    expect(dialog.textContent).toContain(en.v2.policy.publishAttachedNote.replace("{{count}}", "1"));
    for (const id of ["v2-policy-publish-reason", "v2-policy-publish-name", "v2-policy-publish-apply"]) expect(screen.queryByTestId(id)).toBeNull();
    expect(dialog.querySelectorAll("input, textarea")).toHaveLength(0);
    fireEvent.click(screen.getByTestId("v2-policy-confirm-ok"));
    await waitFor(() => expect(publishPolicy).toHaveBeenCalledWith("pe_1", "pol_x", { version: 2 }));
  });
});

describe("PolicyEditor — per-policy enforcement (Global enforcementMode)", () => {
  it("toggles the policy to log_only and back through the engine-scoped route", async () => {
    mount("pol_x");
    const box = (await screen.findByTestId("v2-policy-enforcement-log-only")) as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(screen.getByTestId("v2-policy-enforcement-tag").textContent).toBe(en.v2.governance.policy.enforcement.active);
    detail = { ...DETAIL, enforcement_mode: "log_only" };
    fireEvent.click(box);
    await waitFor(() => expect(setPolicyEnforcementMode).toHaveBeenCalledWith("pe_1", "pol_x", "log_only"));
    await waitFor(() => expect((screen.getByTestId("v2-policy-enforcement-log-only") as HTMLInputElement).checked).toBe(true));
    expect(screen.getByTestId("v2-policy-enforcement-tag").textContent).toBe(en.v2.governance.policy.enforcement.log_only);
    detail = DETAIL;
    fireEvent.click(screen.getByTestId("v2-policy-enforcement-log-only"));
    await waitFor(() => expect(setPolicyEnforcementMode).toHaveBeenLastCalledWith("pe_1", "pol_x", "active"));
  });
});

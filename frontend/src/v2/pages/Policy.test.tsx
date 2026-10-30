// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { GatewayPolicyEngine, Policy, PolicyEngine } from "../../lib/api";
import en from "../../locales/en/common.json";
import { ToastContext } from "../hooks";
import { V2Policy } from "./Policy";

/**
 * The Policy page with the Global AgentCore Policy structure: the engine list (create with
 * Global's name rule), one engine's detail with its four tabs, 附加到网关 (attach this engine to
 * a gateway with a mode / detach — one engine per gateway) and the `?policy=` resolver the chat
 * deny card links through.
 */

let isAdmin = true;
vi.mock("../../auth/auth-context", () => ({
  useAuth: () => ({ isAdmin, can: () => isAdmin, authRequired: true, username: "demo-admin", role: isAdmin ? "admin" : "member" }),
}));
vi.mock("../../workspace/workspace-context", () => ({
  useWorkspace: () => ({ current: { id: "default", name: "default", region: "cn-northwest-1" } }),
}));

const ENGINES: PolicyEngine[] = [
  { id: "pe_hr", name: "hr_engine", description: "HR tools", policy_count: 2,
    attachments: [{ gateway_id: "gw_hr", gateway_name: "hr-gw", mode: "ENFORCE", attached_at: "2026-10-28T09:00:00+08:00" }],
    created_at: "2026-10-28T08:00:00+08:00" },
  { id: "pe_empty", name: "sandbox", description: null, policy_count: 0, has_user_permit: false, attachments: [], created_at: "2026-10-28T08:30:00+08:00" },
];
const POLICIES: Record<string, Policy[]> = {
  pe_hr: [
    { id: "pol_users", engine_id: "pe_hr", name: "hr_users_call_tools", description: null, status: "active", enforcement_mode: "active",
      active_version: 1, draft_version: null, created_by: "admin", created_at: "2026-10-28T08:00:00+08:00", updated_at: "2026-10-28T08:00:00+08:00" },
    { id: "pol_cap", engine_id: "pe_hr", name: "payout_cap", description: "cap payouts", status: "active", enforcement_mode: "log_only",
      active_version: 2, draft_version: 3, created_by: "admin", created_at: "2026-10-28T08:00:00+08:00", updated_at: "2026-10-28T09:00:00+08:00", kind: "temporal" },
  ],
  pe_empty: [],
};
const GATEWAYS = [
  { id: "gw_hr", name: "hr-gw", arn: "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:gateway/gw_hr", status: "READY", managed: true, description: "" },
  { id: "gw_free", name: "free-gw", arn: "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:gateway/gw_free", status: "READY", managed: false, description: "" },
  // the engine / Lambda live only in the workspace's region: a cn-north-1 gateway cannot be attached
  { id: "gw_bj", name: "launchpad-gw-example002", arn: "arn:aws-cn:bedrock-agentcore:cn-north-1:1:gateway/gw_bj", status: "READY", managed: false, description: "" },
  // ruling 24: governed by another environment's hub (t1 seen from default)
  { id: "gw_t1", name: "launchpad-gw-t1", arn: "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:gateway/gw_t1", status: "READY", managed: false, description: "" },
];
const ATTACHMENTS: Record<string, GatewayPolicyEngine> = {
  gw_hr: { attached: true, engine_id: "pe_hr", engine_name: "hr_engine", mode: "ENFORCE", attached_at: "2026-10-28T09:00:00+08:00" },
  gw_free: { attached: false, engine_id: null, engine_name: null, mode: null },
  gw_bj: { attached: false, engine_id: null, engine_name: null, mode: null },
  gw_t1: { attached: true, engine_id: "pe_t1_default", engine_name: "default_policy_engine", mode: "ENFORCE", foreign: true, foreign_lambda: "launchpad-policy-interceptor-t1" },
};
const createPolicyEngine = vi.fn(async (input: { name: string; description: string | null }) => ({ ...ENGINES[1], id: "pe_new", name: input.name, description: input.description }));
const attachPolicyEngine = vi.fn(async (...[, input]: [string, { engine_id: string; mode: string }]): Promise<GatewayPolicyEngine> => ({
  attached: true, engine_id: input.engine_id, engine_name: "hr_engine", mode: input.mode as "LOG_ONLY" | "ENFORCE", attached_at: "2026-10-28T10:00:00+08:00",
  interceptor: { attached: true, lambda_digest: "sha256:abc", bundle_version: 4 },
}));
const detachPolicyEngine = vi.fn<(gatewayId: string) => Promise<GatewayPolicyEngine>>(async () => ({ attached: false, engine_id: null, engine_name: null, mode: null }));
const publishPolicy = vi.fn(async () => POLICIES.pe_hr[1]);
const setPolicyEnforcementMode = vi.fn(async () => POLICIES.pe_hr[1]);
vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listPolicyEngines: vi.fn(async () => ({ engines: ENGINES })),
      getPolicyEngine: vi.fn(async (id: string) => ENGINES.find((e) => e.id === id) ?? Promise.reject(new actual.ApiError("policy_engine.not_found", "no such engine", null))),
      createPolicyEngine: (input: { name: string; description: string | null }) => createPolicyEngine(input),
      listEnginePolicies: vi.fn(async (id: string) => ({ policies: POLICIES[id] ?? [] })),
      policyEntities: vi.fn(async () => ({ users: [], api_keys: [], agents: [], tools: [], templates: [] })),
      listGovernanceGateways: vi.fn(async () => ({ gateways: GATEWAYS, source: "live", cache: null })),
      gatewayPolicyEngine: vi.fn(async (id: string) => ATTACHMENTS[id]),
      attachPolicyEngine: (gatewayId: string, input: { engine_id: string; mode: string }) => attachPolicyEngine(gatewayId, input),
      detachPolicyEngine: (gatewayId: string) => detachPolicyEngine(gatewayId),
      publishPolicy: (...args: unknown[]) => publishPolicy(...(args as [])),
      setPolicyEnforcementMode: (...args: unknown[]) => setPolicyEnforcementMode(...(args as [])),
      policyDecisions: vi.fn(async () => ({ decisions: [], runtime_channel: null })),
      policyChanges: vi.fn(async () => ({ changes: [{ id: "chg_1", policy_id: "pol_cap", version: 3, operation: "policy.publish", operator: "demo-admin", status: "succeeded", before: null, requested: null, after: null, error: null, created_at: "2026-10-28T09:30:00+08:00" }] })),
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
  isAdmin = true;
  createPolicyEngine.mockClear();
  attachPolicyEngine.mockClear();
  detachPolicyEngine.mockClear();
  publishPolicy.mockClear();
  setPolicyEnforcementMode.mockClear();
});

function Location() {
  const { pathname, search } = useLocation();
  return <div data-testid="location">{pathname + search}</div>;
}
const renderAt = (search: string) =>
  render(
    <ToastContext.Provider value={() => undefined}>
      <MemoryRouter initialEntries={[`/v2/policy${search}`]}>
        <Routes>
          <Route path="/v2/policy" element={<><V2Policy /><Location /></>} />
        </Routes>
      </MemoryRouter>
    </ToastContext.Provider>,
  );
const p = en.v2.policy;

describe("V2 Policy — engine list", () => {
  it("lists the engines with their policy counts and gateway attachments (mode), and opens one", async () => {
    renderAt("");
    expect(await screen.findByTestId("v2-policy-engine-open-hr_engine")).toBeTruthy();
    expect(screen.getByTestId("v2-policy-engine-count-hr_engine").textContent).toBe("2");
    const attachments = screen.getByTestId("v2-policy-engine-attachments-hr_engine");
    expect(attachments.textContent).toContain("hr-gw");
    expect(attachments.textContent).toContain(p.attachMode.enforce);
    expect(screen.getByTestId("v2-policy-engine-table").textContent).toContain(p.notAttached);  // sandbox
    // nothing of the retired workspace surface
    for (const id of ["v2-policy-settings", "v2-policy-interceptor", "v2-policy-apply", "v2-policy-enforce-dialog"]) expect(screen.queryByTestId(id)).toBeNull();
    fireEvent.click(screen.getByTestId("v2-policy-engine-open-hr_engine"));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/v2/policy?view=engine&engine=pe_hr"));
  });

  it("creates an engine with Global's name rule and lands on it", async () => {
    renderAt("");
    fireEvent.click(await screen.findByTestId("v2-policy-engine-new"));
    const ok = screen.getByTestId("v2-policy-engine-create-ok") as HTMLButtonElement;
    expect(ok.disabled).toBe(true);
    fireEvent.change(screen.getByTestId("v2-policy-engine-name"), { target: { value: "1bad-name" } });
    expect(ok.disabled).toBe(true);
    expect(screen.getByText(en.v2.governance.policy.invalidName)).toBeTruthy();
    fireEvent.change(screen.getByTestId("v2-policy-engine-name"), { target: { value: "finance_engine" } });
    fireEvent.change(screen.getByTestId("v2-policy-engine-description"), { target: { value: "finance tools" } });
    expect(ok.disabled).toBe(false);
    fireEvent.click(ok);
    await waitFor(() => expect(createPolicyEngine).toHaveBeenCalledWith({ name: "finance_engine", description: "finance tools" }));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/v2/policy?view=engine&engine=pe_new"));
  });

  it("a member cannot create an engine", async () => {
    isAdmin = false;
    renderAt("");
    expect(((await screen.findByTestId("v2-policy-engine-new")) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("V2 Policy — engine detail", () => {
  it("warns when the engine has no active user permit", async () => {
    renderAt("?view=engine&engine=pe_empty");
    expect(await screen.findByText(p.noUserPermit)).toBeTruthy();
  });
  it("shows the four tabs and the policies with their Global enforcementMode; publish is a plain confirm", async () => {
    renderAt("?view=engine&engine=pe_hr");
    await screen.findByTestId("v2-policy-list");
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      `${p.tab.policies} (2)`, p.tab.simulate, p.tab.decisions, p.tab.audit, `${p.tab.gateways} (1)`,
    ]);
    expect(screen.getByTestId("v2-policy-engine-attached").textContent).toBe(p.attachedCount.replace("{{count}}", "1"));
    expect(screen.getByTestId("v2-policy-enforcement-hr_users_call_tools").textContent).toBe(en.v2.governance.policy.enforcement.active);
    expect(screen.getByTestId("v2-policy-enforcement-payout_cap").textContent).toBe(en.v2.governance.policy.enforcement.log_only);
    expect((screen.getByTestId("v2-policy-log-only-payout_cap") as HTMLInputElement).checked).toBe(true);
    expect(screen.getByTestId("v2-policy-kind-payout_cap").textContent).toBe(en.v2.governance.policy.kind.temporal);
    // no scope column / filter any more
    expect(screen.getByTestId("v2-policy-table").textContent).not.toContain("Workspace");
    // the per-policy toggle calls the engine-scoped route
    fireEvent.click(screen.getByTestId("v2-policy-log-only-hr_users_call_tools"));
    await waitFor(() => expect(setPolicyEnforcementMode).toHaveBeenCalledWith("pe_hr", "pol_users", "log_only"));
    // publish the draft v3 of payout_cap: the confirm carries no inputs
    fireEvent.click(screen.getByTestId("v2-policy-publish-payout_cap"));
    const confirm = screen.getByTestId("v2-confirm");
    expect(confirm.textContent).toContain(en.v2.governance.policy.confirmBody.publish.replace("{{name}}", "payout_cap").replace("{{version}}", "3"));
    expect(confirm.querySelectorAll("input, textarea")).toHaveLength(0);
    fireEvent.click(screen.getByTestId("v2-confirm-ok"));
    await waitFor(() => expect(publishPolicy).toHaveBeenCalledWith("pe_hr", "pol_cap", {}));
  });

  it("the 变更日志 tab lists the engine's journal and its drawer opens the policy's editor", async () => {
    renderAt("?view=engine&engine=pe_hr&tab=audit");
    fireEvent.click(await screen.findByTestId("v2-policy-audit-chg_1"));
    expect(screen.getByTestId("v2-policy-audit-drawer").textContent).toContain("policy.publish");
    fireEvent.click(screen.getByTestId("v2-policy-audit-open-editor"));
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/v2/policy?view=policy&engine=pe_hr&policy=pol_cap"));
  });

  it("delete is blocked while the engine is attached", async () => {
    renderAt("?view=engine&engine=pe_hr");
    expect(((await screen.findByTestId("v2-policy-engine-delete")) as HTMLButtonElement).disabled).toBe(true);
  });
});

describe("V2 Policy — 附加到网关", () => {
  it("lists every gateway with its attached engine and mode; attaches this engine to a free gateway with the chosen mode", async () => {
    renderAt("?view=engine&engine=pe_empty&tab=gateways");
    await screen.findByTestId("v2-policy-gateway-table");
    await waitFor(() => expect(screen.getByTestId("v2-policy-gw-engine-gw_hr").textContent).toContain("hr_engine"));
    expect(screen.getByTestId("v2-policy-gw-mode-gw_hr").textContent).toBe(p.attachMode.enforce);
    expect(screen.getByTestId("v2-policy-gw-engine-gw_free").textContent).toBe(p.notAttached);
    // a gateway in another region shows its region, the 「不可附加（跨区域）」 tag and a disabled attach
    expect(screen.getByTestId("v2-policy-gw-region-gw_bj").textContent).toContain("cn-north-1");
    expect(screen.getByTestId("v2-policy-gw-cross-region-gw_bj").textContent).toBe(p.crossRegion);
    expect((screen.getByTestId("v2-policy-gw-attach-gw_bj") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("v2-policy-gw-gw_bj").closest("a")).toBeNull();
    expect(screen.queryByTestId("v2-policy-gw-cross-region-gw_free")).toBeNull();
    // ruling 24: a gateway another environment's hub governs reads 「由其他环境管理」, no controls
    await waitFor(() => expect(screen.getByTestId("v2-policy-gw-foreign-gw_t1").textContent).toBe(p.foreign));
    expect(screen.getByTestId("v2-policy-gw-engine-gw_t1").textContent).toContain("default_policy_engine");
    expect(screen.getByTestId("v2-policy-gw-mode-gw_t1").textContent).toBe(p.attachMode.enforce);
    expect(screen.getByTestId("v2-policy-gw-foreign-actions-gw_t1")).toBeTruthy();
    for (const id of ["v2-policy-gw-attach-gw_t1", "v2-policy-gw-change-gw_t1", "v2-policy-gw-detach-gw_t1"]) expect(screen.queryByTestId(id)).toBeNull();
    // a gateway carrying another engine offers "replace", a free one "attach this engine"
    expect(screen.getByTestId("v2-policy-gw-attach-gw_hr").textContent).toBe(p.attach.replace);
    fireEvent.click(screen.getByTestId("v2-policy-gw-attach-gw_free"));
    const dialog = screen.getByTestId("v2-policy-attach-dialog");
    expect(dialog.textContent).toContain(p.attach.title.replace("{{gateway}}", "free-gw"));
    expect(screen.queryByTestId("v2-policy-attach-engine")).toBeNull();  // the engine is pinned
    fireEvent.click(screen.getByTestId("v2-policy-attach-mode-enforce"));
    expect(dialog.textContent).toContain(p.attach.enforceNote);
    fireEvent.click(screen.getByTestId("v2-policy-attach-ok"));
    await waitFor(() => expect(attachPolicyEngine).toHaveBeenCalledWith("gw_free", { engine_id: "pe_empty", mode: "ENFORCE" }));
  });

  it("detaches this engine from a gateway it is attached to; a member sees the controls disabled", async () => {
    renderAt("?view=engine&engine=pe_hr&tab=gateways");
    await screen.findByTestId("v2-policy-gateway-table");
    const detach = await screen.findByTestId("v2-policy-gw-detach-gw_hr");
    expect(screen.getByTestId("v2-policy-gw-change-gw_hr")).toBeTruthy();
    fireEvent.click(detach);
    expect(screen.getByTestId("v2-confirm").textContent).toContain(p.attach.detachBody.replace("{{engine}}", "hr_engine").replace("{{gateway}}", "hr-gw"));
    fireEvent.click(screen.getByTestId("v2-confirm-ok"));
    await waitFor(() => expect(detachPolicyEngine).toHaveBeenCalledWith("gw_hr"));
    cleanup();
    isAdmin = false;
    renderAt("?view=engine&engine=pe_hr&tab=gateways");
    expect(((await screen.findByTestId("v2-policy-gw-detach-gw_hr")) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText(p.attach.adminOnly)).toBeTruthy();
  });
});

describe("V2 Policy — ?policy= resolver (deny card links)", () => {
  it("finds the engine that owns the policy and lands on its editor, or its decisions tab", async () => {
    renderAt("?policy=pol_cap");
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/v2/policy?view=policy&engine=pe_hr&policy=pol_cap"));
    cleanup();
    renderAt("?policy=pol_cap&tab=decisions");
    await waitFor(() => expect(screen.getByTestId("location").textContent).toBe("/v2/policy?view=engine&engine=pe_hr&tab=decisions"));
  });
  it("says so when no engine owns the policy", async () => {
    renderAt("?policy=pol_gone");
    expect((await screen.findByTestId("v2-policy-resolver-missing")).textContent).toBe(p.policyNotFound.replace("{{id}}", "pol_gone"));
  });
});

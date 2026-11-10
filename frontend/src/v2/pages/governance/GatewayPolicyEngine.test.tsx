// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { GatewayPolicyEngine, GovernanceGatewayDetail } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import { ToastContext } from "../../hooks";
import { GatewayDetail } from "./GatewayDetail";

/**
 * Gateway detail → 策略引擎 section (Global `policyEngineConfiguration`): the attached engine
 * and its mode in the header and the section; 附加 (choose an engine + mode) / 更换 / 移除 call
 * the same `PUT` / `DELETE /api/gateways/{gid}/policy-engine` as the Policy page.
 */

vi.mock("../../../auth/auth-context", () => ({
  useAuth: () => ({ isAdmin: true, can: () => true }),
}));

const GATEWAY: GovernanceGatewayDetail = {
  id: "gw_hr", name: "hr-gw", arn: "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:gateway/gw_hr", status: "READY", managed: true, description: "",
  url: null, authorizer_type: "CUSTOM_JWT", protocol_type: "MCP", role_arn: null, updated_at: "2026-10-28T09:00:00+08:00",
  status_reasons: [], targets: [], actions: [], actions_uncovered_targets: [], external_tools_list_command: null,
} as unknown as GovernanceGatewayDetail;
let attachment: GatewayPolicyEngine = { attached: false, engine_id: null, engine_name: null, mode: null, interceptor: { attached: false } };
const attachPolicyEngine = vi.fn(async (...[, input]: [string, { engine_id: string; mode: string }]): Promise<GatewayPolicyEngine> => ({
  attached: true, engine_id: input.engine_id, engine_name: "hr_engine", mode: input.mode as "LOG_ONLY" | "ENFORCE", attached_at: "2026-10-28T10:00:00+08:00",
  interceptor: { attached: true, bundle_version: 1 },
}));
const detachPolicyEngine = vi.fn<(gatewayId: string) => Promise<GatewayPolicyEngine>>(async () => ({ attached: false, engine_id: null, engine_name: null, mode: null }));
vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getGovernanceGateway: vi.fn(async () => GATEWAY),
      gatewayPolicyEngine: vi.fn(async () => attachment),
      listPolicyEngines: vi.fn(async () => ({
        engines: [
          { id: "pe_hr", name: "hr_engine", description: null, policy_count: 2, attachments: [], created_at: "2026-10-28T08:00:00+08:00" },
          { id: "pe_fin", name: "finance_engine", description: null, policy_count: 0, attachments: [], created_at: "2026-10-28T08:00:00+08:00" },
        ],
      })),
      attachPolicyEngine: (gatewayId: string, input: { engine_id: string; mode: string }) => attachPolicyEngine(gatewayId, input),
      detachPolicyEngine: (gatewayId: string) => detachPolicyEngine(gatewayId),
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
  attachPolicyEngine.mockClear();
  detachPolicyEngine.mockClear();
  attachment = { attached: false, engine_id: null, engine_name: null, mode: null, interceptor: { attached: false } };
});

const mount = () =>
  render(
    <ToastContext.Provider value={() => undefined}>
      <MemoryRouter initialEntries={["/v2/governance?view=gateway&gateway=gw_hr&section=policy-engine"]}>
        <GatewayDetail gatewayId="gw_hr" section="policy-engine" />
      </MemoryRouter>
    </ToastContext.Provider>,
  );
const p = en.v2.policy;

describe("Gateway detail — 策略引擎 section", () => {
  it("a gateway governed by another environment's hub (ruling 24): 「由其他环境管理」 note with the Lambda, every control disabled, no engine link", async () => {
    attachment = { attached: true, engine_id: "pe_t1", engine_name: "default_policy_engine", mode: "ENFORCE", foreign: true, foreign_lambda: "launchpad-policy-interceptor-t1" };
    mount();
    const note = await screen.findByTestId("v2-governance-policy-engine-foreign");
    expect(note.textContent).toContain(p.foreign);
    expect(note.textContent).toContain("launchpad-policy-interceptor-t1");
    expect(screen.queryByText(p.gateway.unguarded)).toBeNull();
    expect((screen.getByTestId("v2-governance-policy-engine-change") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("v2-governance-policy-engine-detach") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("v2-governance-policy-engine-name").tagName).toBe("SPAN");
    expect(screen.getByTestId("v2-governance-policy-engine-tag").textContent).toBe("default_policy_engine · Enforce");
    expect(en.apiErrors["gateway.policy_engine_foreign"]).toBeTruthy();
  });

  it("an unguarded gateway: warns, lets an administrator pick an engine and a mode, posts the attachment", async () => {
    mount();
    await screen.findByTestId("v2-governance-policy-engine");
    expect(await screen.findByText(p.gateway.unguarded)).toBeTruthy();
    expect(screen.getByTestId("v2-governance-policy-engine-name").textContent).toBe(p.notAttached);
    expect(screen.queryByTestId("v2-governance-policy-engine-tag")).toBeNull();
    const attach = screen.getByTestId("v2-governance-policy-engine-attach") as HTMLButtonElement;
    // The parent can supply the warning before this section finishes its own read.
    await waitFor(() => expect(attach.disabled).toBe(false));
    fireEvent.click(attach);
    const ok = screen.getByTestId("v2-policy-attach-ok") as HTMLButtonElement;
    expect(ok.disabled).toBe(true);  // no engine chosen yet
    fireEvent.click(screen.getByTestId("v2-policy-attach-engine"));
    fireEvent.click(screen.getByText("finance_engine"));
    expect(ok.disabled).toBe(false);
    // LOG_ONLY is the default mode
    fireEvent.click(ok);
    await waitFor(() => expect(attachPolicyEngine).toHaveBeenCalledWith("gw_hr", { engine_id: "pe_fin", mode: "LOG_ONLY" }));
    // the header now names the engine and the mode
    await waitFor(() => expect(screen.getByTestId("v2-governance-policy-engine-tag").textContent).toBe("hr_engine · Log only"));
  });

  it("an attached gateway: shows engine, mode and interceptor; 更换 and 移除 are offered, detach calls DELETE", async () => {
    attachment = {
      attached: true, engine_id: "pe_hr", engine_name: "hr_engine", mode: "ENFORCE", attached_at: "2026-10-28T09:30:00+08:00",
      interceptor: { attached: true, lambda_digest: "sha256:d07e4bb4aaaa", bundle_version: 7 },
    };
    mount();
    await screen.findByTestId("v2-governance-policy-engine");
    await waitFor(() => expect(screen.getByTestId("v2-governance-policy-engine-name").textContent).toBe("hr_engine"));
    expect(screen.getByTestId("v2-governance-policy-engine-name").getAttribute("href")).toBe("/v2/policy?view=engine&engine=pe_hr");
    expect(screen.getByTestId("v2-governance-policy-engine-mode").textContent).toBe(p.attachMode.enforce);
    expect(screen.getByTestId("v2-governance-policy-interceptor").textContent).toContain(p.gateway.interceptorAttached);
    expect(screen.getByTestId("v2-governance-policy-interceptor").textContent).toContain(p.gateway.bundle.replace("{{version}}", "7"));
    expect(screen.getByTestId("v2-governance-policy-engine-change")).toBeTruthy();
    fireEvent.click(screen.getByTestId("v2-governance-policy-engine-detach"));
    fireEvent.click(screen.getByTestId("v2-confirm-ok"));
    await waitFor(() => expect(detachPolicyEngine).toHaveBeenCalledWith("gw_hr"));
    await waitFor(() => expect(screen.getByTestId("v2-governance-policy-engine-name").textContent).toBe(p.notAttached));
  });
});

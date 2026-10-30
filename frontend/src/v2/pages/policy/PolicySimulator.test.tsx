// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { Policy, PolicyEngine, PolicyEntities, PolicySimulateRequest, PolicySimulateResult } from "../../../lib/api";
import { setDisplayTimeZone, zonedInputToMs } from "../../../lib/timestamps";
import en from "../../../locales/en/common.json";
import { emptyHistoryRow, historyEvents } from "./engineHistory";
import { PolicySimulator } from "./PolicySimulator";

/**
 * 模拟器 of one engine (Global's Policy Test): `POST /api/policy-engines/{id}/simulate` with
 * `{gateway_id, principal, agent_id, tool, input, at, history[]}` — call_tool only, decided for
 * the chosen gateway — and the Stage A / Stage BT / final answer with the determining policies
 * and the replayed trace. A pattern row sends a CONCRETE example id, never the glob.
 */

const simulate = vi.fn<(engineId: string, input: PolicySimulateRequest) => Promise<PolicySimulateResult>>();
const policyEntities = vi.fn<(engineId: string, gatewayId?: string | null) => Promise<PolicyEntities>>();
vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      simulatePolicy: (engineId: string, input: PolicySimulateRequest) => simulate(engineId, input),
      policyEntities: (engineId: string, gatewayId?: string | null) => policyEntities(engineId, gatewayId),
      listGovernanceGateways: vi.fn(async () => ({
        gateways: [
          { id: "gw_other", name: "other-gw", arn: "a", status: "READY", managed: false, description: "" },
          { id: "gw_hr", name: "hr-gw", arn: "b", status: "READY", managed: true, description: "" },
        ],
        source: "live",
        cache: null,
      })),
    },
  };
});

const sim = en.v2.governance.policy.sim;
const ENGINE: PolicyEngine = {
  id: "pe_1", name: "hr_engine", description: null, policy_count: 1,
  attachments: [{ gateway_id: "gw_hr", gateway_name: "hr-gw", mode: "ENFORCE" }],
  created_at: "2026-10-28T09:00:00+08:00",
};
const entities: PolicyEntities = {
  users: [{ id: "u1", username: "hr-intern", role: "member" }, { id: "u2", username: "demo-admin", role: "admin" }],
  api_keys: [{ id: "key_1", name: "ci-key" }],
  agents: [{ id: "agt_hr", name: "hr-helpdesk", method: "harness", system: false }, { id: "agt_sys", name: "architect", method: "harness", system: true }],
  tools: [
    { name: "hr-database___create_payout", target: "hr-database", pattern: false },
    { name: "hr-approvals___approve_payout", target: "hr-approvals", pattern: false },
    // a spec that declares a whole target shows up as an fnmatch pattern row
    { name: "crm___*", target: "crm", tool: "*", pattern: true },
  ],
};
const policies: Policy[] = [
  { id: "pol_1", engine_id: "pe_1", name: "permit_users_call_tools", description: null, status: "active", enforcement_mode: "active",
    active_version: 1, draft_version: null, created_by: "admin", created_at: "2026-10-14T00:00:00Z", updated_at: "2026-10-14T00:00:00Z" },
];
const ANSWER: PolicySimulateResult = {
  stage_a: "allow", stage_bt: "deny", final: "deny", log_only_would: null,
  determining: ["pol_payout_admin:v2"], errors: [], source_set: "pe_1:v3", policy_versions: {}, warnings: [],
  trace: [
    { tool: "hr-approvals___approve_payout", kind: "response", at_s: 1 },
    { tool: "hr-database___create_payout", kind: "request", at_s: 2, result: { decision: "deny" } },
  ],
};

beforeAll(async () => {
  // jsdom has no layout: the listbox scrolls its active option into view on open
  Element.prototype.scrollIntoView = () => undefined;
  setDisplayTimeZone("Asia/Shanghai");
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en } }, defaultNS: "common", interpolation: { escapeValue: false },
  });
});
afterEach(() => {
  cleanup();
  simulate.mockReset();
  policyEntities.mockReset();
});

// the run button waits for the gateway list (the attached gateway is the default) and for the
// entity catalog of that gateway (`GET /api/policy-engines/{eid}/entities?gateway_id=`)
const renderSim = async (rows: Policy[] = policies) => {
  policyEntities.mockResolvedValue(entities);
  render(
    <MemoryRouter>
      <PolicySimulator engine={ENGINE} policies={rows} />
    </MemoryRouter>,
  );
  await screen.findByText(`hr-gw · ${en.v2.policy.sim.attached}`);
  await waitFor(() => expect(policyEntities).toHaveBeenCalledWith("pe_1", "gw_hr"));
  await waitFor(() => expect((screen.getByTestId("v2-policy-sim-run") as HTMLButtonElement).disabled).toBe(false));
};

describe("PolicySimulator (one engine, call_tool only)", () => {
  it("posts the contract body for the attached gateway, the first user, the first agent and tool, and renders the stages", async () => {
    simulate.mockResolvedValue(ANSWER);
    await renderSim();
    expect(screen.getByTestId("v2-policy-sim-action").textContent).toBe(en.v2.governance.policy.action.call_tool);
    // the attached gateway is listed (and chosen) first, marked attached
    expect(screen.getByTestId("v2-policy-sim-gateway").textContent).toBe(`hr-gw · ${en.v2.policy.sim.attached}`);
    fireEvent.change(screen.getByTestId("v2-policy-sim-engine-input"), { target: { value: '{"employee_id": "EMP-1024", "amount": 420}' } });
    fireEvent.change(screen.getByTestId("v2-policy-sim-engine-at"), { target: { value: "2026-10-26T22:00" } });
    fireEvent.click(screen.getByTestId("v2-policy-sim-history-add"));
    fireEvent.change(screen.getByTestId("v2-policy-sim-history-output-0"), { target: { value: '{"approved": true}' } });
    fireEvent.click(screen.getByTestId("v2-policy-sim-run"));
    expect(simulate).toHaveBeenCalledWith("pe_1", {
      gateway_id: "gw_hr",
      principal: { kind: "user", id: "hr-intern" },
      agent_id: "agt_hr",
      tool: "hr-database___create_payout",
      input: { employee_id: "EMP-1024", amount: 420 },
      // 22:00 Beijing time = 14:00 UTC; the history row sits 20 minutes earlier
      at: "2026-10-26T14:00:00.000Z",
      history: [{ tool: "hr-database___create_payout", kind: "response", input: {}, output: { approved: true }, at: "2026-10-26T13:40:00.000Z" }],
    });
    const result = await screen.findByTestId("v2-policy-sim-engine-result");
    expect(screen.getByTestId("v2-policy-sim-engine-stage-a").textContent).toBe(en.v2.governance.policy.decision.allow);
    expect(screen.getByTestId("v2-policy-sim-engine-stage-bt").textContent).toBe(en.v2.governance.policy.decision.deny);
    expect(screen.getByTestId("v2-policy-sim-engine-final").textContent).toBe(en.v2.governance.policy.decision.deny);
    expect(screen.getByTestId("v2-policy-sim-engine-determining").textContent).toContain("pol_payout_admin:v2");
    expect(screen.getByTestId("v2-policy-sim-engine-trace").textContent).toContain("hr-database___create_payout");
    expect(result.textContent).toContain(sim.engine.stageBt);
    // the retired hub-side knobs are gone: no context, drafts, pin, record or engine toggle
    for (const id of ["v2-policy-sim-context", "v2-policy-sim-drafts", "v2-policy-sim-policy", "v2-policy-sim-record", "v2-policy-sim-engine"]) {
      expect(screen.queryByTestId(id)).toBeNull();
    }
  });

  it("offers user / API key / system principals (never agent) and only non-system calling agents; warns when the engine has no active policy", async () => {
    simulate.mockResolvedValue({ ...ANSWER, determining: [] });
    await renderSim([]);
    expect(screen.getByText(en.v2.policy.sim.noActive)).toBeTruthy();
    fireEvent.click(screen.getByTestId("v2-policy-sim-principal-kind"));
    expect(screen.getByText(en.v2.governance.policy.principal.api_key)).toBeTruthy();
    expect(screen.getByText(en.v2.governance.policy.principal.system)).toBeTruthy();
    expect(screen.queryByText("Agent")).toBeNull();
    fireEvent.click(screen.getByTestId("v2-policy-sim-principal-kind"));
    fireEvent.click(screen.getByTestId("v2-policy-sim-engine-agent"));
    expect(screen.queryByText("architect")).toBeNull();
    fireEvent.click(screen.getByTestId("v2-policy-sim-engine-agent"));
    fireEvent.click(screen.getByTestId("v2-policy-sim-run"));
    await screen.findByTestId("v2-policy-sim-engine-result");
    expect(screen.getByText(sim.defaultDeny)).toBeTruthy();
    expect(simulate).toHaveBeenCalledWith("pe_1", expect.objectContaining({ at: null, history: [] }));
  });

  it("refuses a bad input or history row without calling the server", async () => {
    await renderSim();
    fireEvent.change(screen.getByTestId("v2-policy-sim-engine-input"), { target: { value: "[]" } });
    fireEvent.click(screen.getByTestId("v2-policy-sim-run"));
    expect(screen.getByText(sim.engine.inputInvalid)).toBeTruthy();
    fireEvent.change(screen.getByTestId("v2-policy-sim-engine-input"), { target: { value: "{}" } });
    fireEvent.click(screen.getByTestId("v2-policy-sim-history-add"));
    fireEvent.change(screen.getByTestId("v2-policy-sim-history-minutes-0"), { target: { value: "-5" } });
    fireEvent.click(screen.getByTestId("v2-policy-sim-run"));
    expect(screen.getByText(sim.engine.historyInvalid.replace("{{row}}", "1"))).toBeTruthy();
    expect(simulate).not.toHaveBeenCalled();
  });

  it("marks pattern rows and simulates a concrete example name for them", async () => {
    simulate.mockResolvedValue({ ...ANSWER, stage_bt: "allow", final: "allow", determining: ["pol_crm:v1"], trace: [] });
    await renderSim();
    expect(screen.queryByTestId("v2-policy-sim-example")).toBeNull();
    fireEvent.click(screen.getByTestId("v2-policy-sim-resource"));
    fireEvent.click(screen.getByText(`crm___* ${sim.patternSuffix}`));
    const example = screen.getByTestId("v2-policy-sim-example") as HTMLInputElement;
    expect(example.value).toBe("crm___example");
    fireEvent.click(screen.getByTestId("v2-policy-sim-run"));
    expect(simulate).toHaveBeenLastCalledWith("pe_1", expect.objectContaining({ tool: "crm___example" }));
    await screen.findByTestId("v2-policy-sim-engine-result");
    fireEvent.change(example, { target: { value: "crm___listOrders" } });
    fireEvent.click(screen.getByTestId("v2-policy-sim-run"));
    expect(simulate).toHaveBeenLastCalledWith("pe_1", expect.objectContaining({ tool: "crm___listOrders" }));
    expect(simulate.mock.calls.some(([, body]) => body.tool.includes("*"))).toBe(false);
  });
});

describe("engine history + display-zone input helpers", () => {
  it("reads a datetime-local value as a display-zone wall clock", () => {
    expect(zonedInputToMs("2026-10-26T22:00")).toBe(Date.UTC(2026, 9, 26, 14, 0));
    expect(zonedInputToMs("not a time")).toBeNull();
  });

  it("orders history events in time and reports the first bad row", () => {
    const at = Date.UTC(2026, 9, 26, 14, 0);
    const rows = [
      { ...emptyHistoryRow("a"), minutesBefore: "5" },
      { ...emptyHistoryRow("b"), minutesBefore: "30" },
    ];
    const events = historyEvents(rows, at);
    expect(Array.isArray(events) && events.map((e) => e.tool)).toEqual(["b", "a"]);
    expect(historyEvents([...rows, { ...emptyHistoryRow(""), minutesBefore: "1" }], at)).toEqual({ invalid: 2 });
    expect(historyEvents([{ ...emptyHistoryRow("a"), output: "nope" }], at)).toEqual({ invalid: 0 });
  });
});

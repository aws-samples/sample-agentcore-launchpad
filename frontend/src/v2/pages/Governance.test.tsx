// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import en from "../../locales/en/common.json";
import { V2Governance } from "./Governance";
import { classicGovernanceToV2 } from "./governance/classicUrl";

/**
 * Global console structure (2026-10-28): the Gateway page carries the Gateways and Tools tabs
 * only — the Policy module is its own top-level page (`/v2/policy`). A classic deep link to
 * the retired policy tabs / views lands on the Policy page with its policy id carried over.
 */

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listGovernanceGateways: vi.fn(async () => ({ gateways: [], source: "live", cache: null })),
      governanceToolCatalog: vi.fn(async () => ({ tools: [], gateway_url: null })),
    },
  };
});
vi.mock("./governance/GatewayList", () => ({ GatewayList: () => <div data-testid="gateway-list" /> }));
vi.mock("./governance/Tools", () => ({ ToolsTab: () => <div data-testid="tools-tab" /> }));

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en } }, defaultNS: "common", interpolation: { escapeValue: false },
  });
});
afterEach(cleanup);

const renderAt = (search: string) =>
  render(
    <MemoryRouter initialEntries={[`/v2/governance${search}`]}>
      <V2Governance />
    </MemoryRouter>,
  );

describe("V2 Governance — Gateways and Tools only", () => {
  it("renders exactly the Gateways and Tools tabs; a retired ?tab=policies deep link falls back to the inventory", () => {
    renderAt("?tab=policies");
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([en.v2.governance.tab.gateways, en.v2.governance.tab.tools]);
    expect(screen.getByTestId("gateway-list")).toBeTruthy();
    expect(screen.queryByTestId("v2-policy-settings")).toBeNull();
    expect(screen.queryByTestId("v2-policy-interceptor")).toBeNull();
    expect(en.v2.governance.tab).not.toHaveProperty("policies");
    expect(en.v2.governance.tab).not.toHaveProperty("audit");
  });

  it("the Tools tab renders", () => {
    renderAt("?tab=tools");
    expect(screen.getByTestId("tools-tab")).toBeTruthy();
  });
});

describe("classicGovernanceToV2 — policy views move to /v2/policy", () => {
  it("maps the retired policy tabs and views onto the Policy page, carrying the policy id", () => {
    expect(classicGovernanceToV2("?view=policies")).toBe("/v2/policy");
    expect(classicGovernanceToV2("?tab=policies")).toBe("/v2/policy");
    expect(classicGovernanceToV2("?view=audit")).toBe("/v2/policy");
    expect(classicGovernanceToV2("?view=simulate")).toBe("/v2/policy");
    expect(classicGovernanceToV2("?view=decisions")).toBe("/v2/policy?tab=decisions");
    expect(classicGovernanceToV2("?view=policy&policy=pol_ab12")).toBe("/v2/policy?policy=pol_ab12");
  });
  it("keeps the gateway deep links, including the new policy-engine section", () => {
    expect(classicGovernanceToV2("")).toBe("/v2/governance");
    expect(classicGovernanceToV2("?view=tools")).toBe("/v2/governance?tab=tools");
    expect(classicGovernanceToV2("?view=gateway&gateway=gw_1")).toBe("/v2/governance?view=gateway&gateway=gw_1");
    expect(classicGovernanceToV2("?view=gateway&gateway=gw_1&section=policy-engine")).toBe("/v2/governance?view=gateway&gateway=gw_1&section=policy-engine");
    expect(classicGovernanceToV2("?view=gateway")).toBe("/v2/governance");
  });
});

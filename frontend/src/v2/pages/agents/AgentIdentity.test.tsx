// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { AgentIdentityDownstream, AgentIdentityInfo } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import zhCN from "../../../locales/zh-CN/common.json";
import { AgentIdentity } from "./AgentIdentity";

/**
 * N17-obs115: `/v2/agents?view=identity` rendered the RAW key
 * `v2.agents.identity.connection.n/a` for a Gateway row — the backend's
 * `connection_status` has four values (`agent_identity.py`: ready / missing / unbound,
 * and `n/a` for the shared Gateway the agent reaches over SigV4 as itself) and the
 * locale knew three. Every backend value must render a label in both locales.
 */
const STATUSES: AgentIdentityDownstream["connection_status"][] = ["ready", "missing", "unbound", "n/a"];

function downstream(status: AgentIdentityDownstream["connection_status"]): AgentIdentityDownstream {
  const gateway = status === "n/a";
  return {
    type: gateway ? "gateway" : "tool",
    name: `ds-${status.replace("/", "_")}`,
    tool_type: gateway ? "gateway" : "mcp",
    via: "agent",
    mode: "as_agent",
    connection: gateway || status === "unbound" ? null : `conn-${status}`,
    kind: gateway ? "aws_iam" : "oauth2",
    scopes: [],
    connection_status: status,
  } as AgentIdentityDownstream;
}

const identity: AgentIdentityInfo = {
  agent_id: "a1",
  name: "hr-helpdesk",
  method: "harness",
  workload_identity: { status: "ready", name: "wl-hr", arn: null, allowed_return_urls: [] },
  inbound: { mode: "iam", source: "aws", invoke_url: null } as AgentIdentityInfo["inbound"],
  downstreams: STATUSES.map(downstream),
};

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return { ...actual, api: { ...actual.api, agentIdentity: vi.fn(async () => identity) } };
});

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zhCN } }, defaultNS: "common", interpolation: { escapeValue: false },
  });
});
afterEach(cleanup);

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={["/v2/agents?view=identity&id=a1"]}>
      <AgentIdentity id="a1" />
    </MemoryRouter>,
  );

describe("identity view — connection status labels (N17-obs115)", () => {
  it.each(["en", "zh-CN"] as const)("renders a label, never a raw key, for every backend status (%s)", async (lng) => {
    await i18next.changeLanguage(lng);
    renderPage();
    const page = await screen.findByTestId("v2-agent-identity-page");
    const labels = (lng === "en" ? en : zhCN).v2.agents.identity.connection as Record<string, string>;
    for (const status of STATUSES) {
      expect(labels[status], `locale ${lng} has a label for ${status}`).toBeTruthy();
      expect(page.textContent).toContain(labels[status]);
    }
    expect(page.textContent).not.toMatch(/v2\.agents\.identity\./);
    expect(page.textContent).not.toMatch(/\bn\/a\b/);
  });

  it("the locale map covers exactly the backend's status values", () => {
    expect(Object.keys(en.v2.agents.identity.connection).sort()).toEqual([...STATUSES].sort());
    expect(Object.keys(zhCN.v2.agents.identity.connection).sort()).toEqual([...STATUSES].sort());
  });
});

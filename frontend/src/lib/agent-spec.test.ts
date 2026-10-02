import { describe, expect, it } from "vitest";

import {
  type AgentFormCatalogs,
  agentFormValid,
  authToolIssues,
  buildAgentSpec,
  type ConnectionRef,
  formFromStoredSpec,
  republishSpec,
} from "./agent-spec";

const CATALOGS: AgentFormCatalogs = {
  gatewayTargets: [],
  remoteMcp: [],
  storedGatewayConfig: {},
  kbInfo: (id) => ({ kb_id: id, name: id, description: "" }),
};
const CONNECTIONS: ConnectionRef[] = [
  { name: "team-idp", kind: "oauth2", status: "ready" },
  { name: "team-key", kind: "api_key", status: "ready" },
];

const CRM = {
  type: "rest",
  name: "crm",
  config: { url: "https://crm.example/api", method: "POST" },
  auth: { connection: "team-idp", kind: "oauth2", mode: "as_agent", scopes: ["crm/read"], audience: null, api_key: null },
};
const FACTS = {
  type: "rest",
  name: "facts",
  config: { url: "https://facts.example" },
  auth: { connection: "team-key", kind: "api_key", mode: "as_agent", scopes: [], api_key: { in: "query", name: "key" } },
};
const STORED = {
  name: "auth-agent",
  method: "zip_runtime",
  system_prompt: "Be helpful.",
  model_id: "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
  tools: [CRM, FACTS, { type: "mcp", name: "docs", config: { url: "https://docs.example/mcp" } }],
};

const load = (stored: object) => formFromStoredSpec("zip_runtime", "auth-agent", stored).form;
const valid = (form: ReturnType<typeof load>, connections: ConnectionRef[] | null) =>
  agentFormValid(form, CATALOGS, { knobIssues: [], byocUploading: false, connections });

describe("tool-level auth round trip", () => {
  it("routes auth-carrying tools to the Identity rows, not the MCP picks", () => {
    const form = load(STORED);
    expect(form.authTools.map((r) => [r.type, r.name, r.connection, r.kind])).toEqual([
      ["rest", "crm", "team-idp", "oauth2"],
      ["rest", "facts", "team-key", "api_key"],
    ]);
    expect(form.authTools[0].extraConfig).toEqual({ method: "POST" });
    expect(form.selectedMcp).toEqual(["docs"]);
  });

  it("keeps every auth block when an unrelated field is edited and republished", () => {
    const form = { ...load(STORED), systemPrompt: "Now answer in French." };
    expect(valid(form, CONNECTIONS)).toBe(true);
    const spec = republishSpec(buildAgentSpec(form, CATALOGS), STORED);
    expect(spec.system_prompt).toBe("Now answer in French.");
    const byName = Object.fromEntries((spec.tools ?? []).map((tool) => [tool.name, tool]));
    expect(byName.crm.auth).toEqual({ connection: "team-idp", kind: "oauth2", mode: "as_agent", scopes: ["crm/read"] });
    expect(byName.crm.config).toEqual({ url: "https://crm.example/api", method: "POST" });
    expect(byName.facts.auth).toEqual({
      connection: "team-key",
      kind: "api_key",
      mode: "as_agent",
      scopes: [],
      api_key: { in: "query", name: "key" },
    });
  });

  it("maps the earlier fork's provider/flow shape", () => {
    const form = load({ ...STORED, tools: [{ ...CRM, auth: { provider: "team-key", flow: "API_KEY" } }] });
    expect(form.authTools[0]).toMatchObject({ connection: "team-key", kind: "api_key", mode: "as_agent" });
  });
});

describe("republish carry-through", () => {
  it("fills in auth only for a built tool that does not state it", () => {
    const built = { name: "auth-agent", method: "zip_runtime" as const, system_prompt: "p", tools: [{ type: "rest", name: "crm" }] };
    expect(republishSpec(built, STORED).tools?.[0].auth).toEqual(CRM.auth);
  });

  it("never back-fills a row the member turned open on purpose", () => {
    const form = load(STORED);
    form.authTools[0] = { ...form.authTools[0], connection: "" };
    const tools = republishSpec(buildAgentSpec(form, CATALOGS), STORED).tools ?? [];
    expect(tools.find((tool) => tool.name === "crm")?.auth).toBeNull();
  });
});

describe("a missing Connection blocks submission", () => {
  it("flags the row and refuses the form when the Connection left the vault", () => {
    const form = load(STORED);
    const gone = CONNECTIONS.filter((c) => c.name !== "team-idp");
    expect(authToolIssues(form, gone).rows[0]).toEqual({ connection: "missing" });
    expect(valid(form, gone)).toBe(false);
    // ...and the row is still there, not stripped
    expect(buildAgentSpec(form, CATALOGS).tools?.find((t) => t.name === "crm")?.auth?.connection).toBe("team-idp");
  });

  it("treats an audit row whose provider is missing as absent", () => {
    const form = load(STORED);
    const stale: ConnectionRef[] = [{ ...CONNECTIONS[0], status: "missing" }, CONNECTIONS[1]];
    expect(valid(form, stale)).toBe(false);
  });

  it("refuses while the catalog is unavailable", () => {
    const form = load(STORED);
    expect(authToolIssues(form, null).catalogPending).toBe(true);
    expect(valid(form, null)).toBe(false);
  });

  it("refuses a kind mismatch and an unimplemented acting mode", () => {
    const mismatched = load({ ...STORED, tools: [{ ...CRM, auth: { ...CRM.auth, kind: "api_key", scopes: [] } }] });
    expect(authToolIssues(mismatched, CONNECTIONS).rows[0]).toEqual({ connection: "kind" });
    const obo = load({ ...STORED, tools: [{ ...CRM, auth: { ...CRM.auth, mode: "obo" } }] });
    expect(authToolIssues(obo, CONNECTIONS).rows[0]).toEqual({ mode: true });
    expect(valid(obo, CONNECTIONS)).toBe(false);
  });

  it("accepts as_user on an OAuth2 Connection only", () => {
    const asUser = load({ ...STORED, tools: [{ ...CRM, auth: { ...CRM.auth, mode: "as_user" } }] });
    expect(authToolIssues(asUser, CONNECTIONS).rows[0]).toBeUndefined();
    expect(buildAgentSpec(asUser, CATALOGS).tools?.find((t) => t.name === "crm")?.auth?.mode).toBe("as_user");
    const keyAsUser = load({ ...STORED, tools: [{ ...FACTS, auth: { ...FACTS.auth, mode: "as_user" } }] });
    expect(authToolIssues(keyAsUser, CONNECTIONS).rows[0]).toEqual({ mode: true });
  });

  it("accepts an open rest tool with no catalog at all", () => {
    const form = load({ ...STORED, tools: [{ type: "rest", name: "open", config: { url: "https://open.example" } }] });
    expect(valid(form, null)).toBe(true);
  });

  it("refuses auth rows where nothing would send them", () => {
    const form = { ...load(STORED), protocol: "a2a" as const };
    expect(authToolIssues(form, CONNECTIONS).unsupported).toBe(true);
    expect(valid(form, CONNECTIONS)).toBe(false);
  });
});

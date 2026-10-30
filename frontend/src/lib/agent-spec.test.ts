import { describe, expect, it } from "vitest";

import {
  AGENT_NAME_RE,
  type AgentFormCatalogs,
  agentFormValid,
  authToolIssues,
  buildAgentSpec,
  builtinOffered,
  type ConnectionRef,
  flagOffered,
  formFromStoredSpec,
  optionOffered,
  republishSpec,
} from "./agent-spec";

const CATALOGS: AgentFormCatalogs = {
  gatewayTargets: [],
  remoteMcp: [],
  agents: [],
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

describe("agent name rule", () => {
  it("is the backend's `AgentSpec.name` rule, anchored at both ends (mutation baseline N16-obs62)", () => {
    expect(AGENT_NAME_RE.test("1abc")).toBe(false); // first char a lower-case letter
    expect(AGENT_NAME_RE.test("abc_d")).toBe(false); // whole string, not a prefix
    expect(AGENT_NAME_RE.test("ab")).toBe(false); // 3 … 48 chars
    expect(AGENT_NAME_RE.test("a".repeat(49))).toBe(false);
  });
});

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

  it("a re-publish carries the stored model_region / model_credential — no form input owns them (R15)", () => {
    const built = buildAgentSpec(load(STORED), CATALOGS);
    expect(built.model_region).toBeUndefined();
    expect(built.model_credential).toBeUndefined();
    const spec = republishSpec(built, { ...STORED, model_region: "us-east-1", model_credential: "team-bedrock-key" });
    expect(spec.model_region).toBe("us-east-1");
    expect(spec.model_credential).toBe("team-bedrock-key");
    expect(republishSpec(built, STORED).model_region).toBeUndefined();
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

// N10v2-bug5: mounting another Launchpad agent by its APPROVED A2A record.
const PEER_ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/aurora_faq-abc123";
const AURORA = {
  name: "aurora-faq-a2a",
  description: "Aurora product FAQ",
  record_id: "rec-1",
  agent_id: "agent0001",
  runtime_arn: PEER_ARN,
  url: "https://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/runtimes/x/invocations",
  skills: [{ id: "faq", name: "FAQ", description: "" }],
  attachable: true,
  attachability_reason: null,
};
const WITH_AGENTS: AgentFormCatalogs = { ...CATALOGS, agents: [AURORA] };

describe("A2A agent attachments", () => {
  it("builds an a2a ToolRef from the catalog pick for harness and Strands (http)", () => {
    for (const method of ["harness", "zip_runtime"] as const) {
      const form = { ...load({ name: "router", method, system_prompt: "p" }), selectedAgents: ["aurora-faq-a2a"] };
      const tool = buildAgentSpec(form, WITH_AGENTS).tools?.find((t) => t.type === "a2a");
      expect(tool).toEqual({
        type: "a2a",
        name: "aurora-faq-a2a",
        config: { record_id: "rec-1", runtime_arn: PEER_ARN, agent_id: "agent0001", url: AURORA.url, description: "Aurora product FAQ" },
      });
    }
  });

  it("never sends one for the A2A server protocol — the template has no tool channel", () => {
    const form = { ...load({ name: "srv", method: "zip_runtime", system_prompt: "p", protocol: "a2a" }), selectedAgents: ["aurora-faq-a2a"] };
    expect(buildAgentSpec(form, WITH_AGENTS).tools ?? []).toEqual([]);
  });

  it("round-trips a stored a2a attachment and keeps its config when the record left the catalog", () => {
    const stored = {
      name: "router",
      method: "harness",
      system_prompt: "p",
      tools: [{ type: "a2a", name: "aurora-faq-a2a", config: { record_id: "rec-1", runtime_arn: PEER_ARN, agent_id: "agent0001" } }],
    };
    const { form, extras } = formFromStoredSpec("harness", "router", stored);
    expect(form.selectedAgents).toEqual(["aurora-faq-a2a"]);
    expect(extras.storedA2aConfig["aurora-faq-a2a"]).toEqual({ record_id: "rec-1", runtime_arn: PEER_ARN, agent_id: "agent0001" });
    // catalog gone (record disabled) → the stored config still re-publishes verbatim
    const kept = buildAgentSpec(form, { ...CATALOGS, storedA2aConfig: extras.storedA2aConfig }).tools?.[0];
    expect(kept).toEqual(stored.tools[0]);
    // neither catalog nor stored config → the pick is dropped rather than sent half-formed
    expect(buildAgentSpec(form, CATALOGS).tools ?? []).toEqual([]);
    // a live catalog entry wins over the stored config
    const live = buildAgentSpec(form, WITH_AGENTS).tools?.[0];
    expect(live?.config).toMatchObject({ url: AURORA.url, description: "Aurora product FAQ" });
  });
});

describe("options offered per deploy method (N15-obs16 → round 16)", () => {
  const caps = {
    harness: {
      builtin_tools: ["code-interpreter"], tool_types: ["builtin", "gateway", "mcp", "a2a"], tool_auth_modes: [],
      native_tools: ["shell", "file_operations"], protocols: ["http"], memory: true, knowledge_bases: true,
      byoc_kinds: [], requires: ["artifacts_bucket"],
    },
    zip_runtime: {
      builtin_tools: [], tool_types: ["gateway", "rest", "a2a"], tool_auth_modes: ["as_agent", "as_user"],
      native_tools: [], protocols: ["http", "a2a"], memory: true, knowledge_bases: true,
      byoc_kinds: [], requires: ["artifacts_bucket"],
    },
  };
  it("offers an option only where the method declares it, and everything while unknown", () => {
    expect(builtinOffered("code-interpreter", "harness", caps)).toBe(true);
    expect(builtinOffered("browser", "harness", caps)).toBe(false);
    expect(builtinOffered("code-interpreter", "zip_runtime", caps)).toBe(false);
    expect(builtinOffered("browser", "harness", null)).toBe(true);
    expect(optionOffered(caps, "zip_runtime", "protocols", "a2a")).toBe(true);
    expect(optionOffered(caps, "harness", "protocols", "a2a")).toBe(false);
    expect(optionOffered(caps, "harness", "native_tools", "shell")).toBe(true);
    expect(optionOffered(caps, "zip_runtime", "native_tools", "shell")).toBe(false);
    expect(flagOffered(caps, "harness", "knowledge_bases")).toBe(true);
    // a method the server does not list, or no declaration at all, offers everything
    expect(optionOffered(caps, "byoc", "byoc_kinds", "code_zip")).toBe(true);
    expect(optionOffered(null, "harness", "builtin_tools", "new-tool")).toBe(true);
  });
});

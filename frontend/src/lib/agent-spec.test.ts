import { describe, expect, it } from "vitest";

import {
  type AgentFormCatalogs,
  agentFormValid,
  authToolIssues,
  buildAgentSpec,
  buildMcpServerSpec,
  byocIssues,
  type ConnectionRef,
  contextLayerSpec,
  contextLayerVia,
  managedGatewayAttached,
  emptyAgentForm,
  filesystemIssues,
  filesystemValid,
  formFromStoredSpec,
  hasByoMounts,
  republishSpec,
  storeCount,
  withContextVia,
} from "./agent-spec";
import { formFromSharedTemplate } from "../v2/pages/agents/templateForms";

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

describe("Runtime platform and billing round trip", () => {
  const baseline = { platform_version: "V2" as const, billing_model: "committed_baseline" as const, minimum_memory_mib: 4096 };

  it.each(["zip_runtime", "container", "byoc"] as const)("defaults a new %s agent to V2/consumption", (method) => {
    const form = { ...emptyAgentForm(method), name: "runtime-agent", systemPrompt: "Help." };
    expect(buildAgentSpec(form, CATALOGS).runtime_config).toEqual({
      platform_version: "V2", billing_model: "consumption", minimum_memory_mib: null,
    });
  });

  it("uses new-agent defaults for a shared template rather than legacy edit semantics", () => {
    const template = { id: "t1", title: "Demo", summary: "", method: "zip_runtime", spec: STORED,
      requirements: [], source_workspace_id: "ws-1", source_agent_name: "source", published_by: null,
      uses: 0, own: false, created_at: null, updated_at: null };
    const fresh = formFromSharedTemplate(template);
    expect(fresh.runtimeConfig).toEqual(emptyAgentForm("zip_runtime").runtimeConfig);
    expect(formFromSharedTemplate({ ...template, spec: { ...STORED, runtime_config: baseline } }).runtimeConfig).toEqual(baseline);
  });

  it("never posts Runtime choices for a Harness", () => {
    expect(buildAgentSpec({ ...emptyAgentForm("harness"), runtimeConfig: baseline }, CATALOGS)).not.toHaveProperty("runtime_config");
  });

  it.each(["zip_runtime", "container", "byoc"] as const)("hydrates and keeps a %s baseline on unrelated edits", (method) => {
    const stored = { ...STORED, method, runtime_config: baseline };
    const form = formFromStoredSpec(method, "runtime-agent", stored).form;
    expect(form.runtimeConfig).toEqual(baseline);
    const built = buildAgentSpec({ ...form, systemPrompt: "Changed prompt." }, CATALOGS);
    expect(republishSpec(built, stored).runtime_config).toEqual(baseline);
  });

  it("leaves a legacy edit unconfigured rather than installing the fresh default", () => {
    const form = load(STORED);
    expect(form.runtimeConfig).toBeNull();
    expect(buildAgentSpec(form, CATALOGS)).not.toHaveProperty("runtime_config");
    expect(republishSpec(buildAgentSpec(form, CATALOGS), STORED)).not.toHaveProperty("runtime_config");
  });

  it("carries a stored baseline for forms without these controls", () => {
    const form = { ...load(STORED), runtimeConfig: undefined };
    const stored = { ...STORED, runtime_config: baseline };
    expect(republishSpec(buildAgentSpec(form, CATALOGS), stored).runtime_config).toEqual(baseline);
  });

  it("makes an explicit switch to consumption override the carried baseline", () => {
    const runtimeConfig = { platform_version: "V1" as const, billing_model: "consumption" as const, minimum_memory_mib: null };
    const form = { ...load(STORED), runtimeConfig };
    expect(republishSpec(buildAgentSpec(form, CATALOGS), { ...STORED, runtime_config: baseline }).runtime_config).toEqual(runtimeConfig);
  });

  it("blocks submission until a committed size is chosen, and refuses V1/baseline", () => {
    const form = { ...load({ ...STORED, tools: [] }), runtimeConfig: { ...baseline, minimum_memory_mib: null } };
    expect(valid(form, [])).toBe(false);
    expect(valid({ ...form, runtimeConfig: baseline }, [])).toBe(true);
    expect(valid({ ...form, runtimeConfig: { ...baseline, platform_version: "V1" } }, [])).toBe(false);
  });

  it("also posts the selected configuration for hosted MCP servers", () => {
    const form = { ...emptyAgentForm("byoc"), protocol: "mcp" as const, mcpTargetName: "tools", byocUploadId: "up-1", runtimeConfig: baseline };
    expect(buildMcpServerSpec(form).runtime_config).toEqual(baseline);
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
  it("carries a converted agent's requirement ranges and pins", () => {
    const converted = {
      ...STORED,
      requirements: ["botocore[crt]==1.43.114", "mcp==1.30.0"],
      requirement_ranges: ["botocore[crt] >= 1.35.0", "mcp >= 1.19.0"],
      source_harness: { agent_id: "src", agent_name: "aurora-support", harness_arn: "arn:h" },
    };
    const built = buildAgentSpec(load(converted), CATALOGS);
    const spec = republishSpec(built, converted) as unknown as Record<string, unknown>;
    expect(spec.requirement_ranges).toEqual(["botocore[crt] >= 1.35.0", "mcp >= 1.19.0"]);
    expect(spec.requirements).toEqual(["botocore[crt]==1.43.114", "mcp==1.30.0"]);
    expect(spec.source_harness).toEqual(converted.source_harness);
  });

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

describe("MCP stdio wrapper (§15.5)", () => {
  const server = () => ({
    ...emptyAgentForm("byoc"),
    name: "stdio_srv",
    protocol: "mcp" as const,
    mcpTargetName: "crm",
    byocUploadId: "up-1",
  });
  const on = { on: true, base: "node" as const, command: "node", args: "build/index.js" };

  it("writes stdio_wrapper only for a code zip with the switch on", () => {
    expect(buildMcpServerSpec(server()).mcp).toEqual({ target_name: "crm" });
    const spec = buildMcpServerSpec({ ...server(), mcpStdio: on });
    expect(spec.mcp).toEqual({ target_name: "crm", stdio_wrapper: { command: "node", args: ["build/index.js"], base: "node" } });
    const image = buildMcpServerSpec({ ...server(), mcpStdio: on, byocKind: "container_image", byocImageUri: "x" });
    expect(image.mcp).toEqual({ target_name: "crm" });
  });

  it("reads the wrapper back and stops asking for a .py entrypoint", () => {
    const stored = { protocol: "mcp", mcp: { target_name: "crm", stdio_wrapper: { command: "node", args: ["a.js"], base: "node" } } };
    expect(formFromStoredSpec("byoc", "stdio_srv", { ...stored, byoc: { artifact_kind: "code_zip" } }).form.mcpStdio).toEqual({ on: true, base: "node", command: "node", args: "a.js" });
    expect(formFromStoredSpec("byoc", "stdio_srv", { protocol: "mcp", mcp: { target_name: "crm" } }).form.mcpStdio?.on).toBe(false);
    expect(byocIssues({ ...server(), byocEntrypoint: "index.js" }).entrypoint).toBe(true);
    expect(byocIssues({ ...server(), byocEntrypoint: "index.js", mcpStdio: on }).entrypoint).toBe(false);
  });
});

describe("container network profile", () => {
  const stored = {
    model_id: "m",
    filesystem: { session_storage: null, s3_files: [{ access_point_arn: "arn:aws:s3files:us-east-1:1:ap/x", mount_path: "/mnt/data" }] },
    network: { profile_id: "np-1" },
  };

  it("keeps a stored profile reference instead of inline ids", () => {
    const form = formFromStoredSpec("container", "c1", stored).form;
    expect(form.networkProfileId).toBe("np-1");
    expect(buildAgentSpec(form, CATALOGS).network).toEqual({ profile_id: "np-1" });
  });

  it("a legacy inline network stays inline", () => {
    const form = formFromStoredSpec("container", "c1", {
      ...stored,
      network: { subnets: ["subnet-0a", "subnet-0b"], security_groups: ["sg-0a"] },
    }).form;
    expect(form.networkProfileId).toBe("");
    expect(buildAgentSpec(form, CATALOGS).network).toEqual({ subnets: ["subnet-0a", "subnet-0b"], security_groups: ["sg-0a"] });
  });
});

describe("network for every method (projects §12.6)", () => {
  it("every method carries a chosen profile; no profile is PUBLIC (omitted)", () => {
    for (const method of ["harness", "zip_runtime", "container"] as const) {
      const form = { ...emptyAgentForm(method), name: "net-a", systemPrompt: "hi" };
      expect(buildAgentSpec(form, CATALOGS).network).toBeUndefined();
      expect(buildAgentSpec({ ...form, networkProfileId: "np-1" }, CATALOGS).network).toEqual({ profile_id: "np-1" });
    }
    const byoc = { ...emptyAgentForm("byoc"), name: "net-b", byocUploadId: "u1", networkProfileId: "np-2" };
    expect(buildAgentSpec(byoc, CATALOGS).network).toEqual({ profile_id: "np-2" });
    const server = { ...byoc, protocol: "mcp" as const, mcpTargetName: "crm" };
    expect(buildMcpServerSpec(server).network).toEqual({ profile_id: "np-2" });
  });

  it("a stored profile round-trips on a harness re-publish, and leaving it drops the field", () => {
    const form = formFromStoredSpec("harness", "h1", { model_id: "m", network: { profile_id: "np-9" } }).form;
    expect(form.networkProfileId).toBe("np-9");
    expect(buildAgentSpec(form, CATALOGS).network).toEqual({ profile_id: "np-9" });
    expect(buildAgentSpec({ ...form, networkProfileId: "" }, CATALOGS).network).toBeUndefined();
  });

  it("VPC chosen without a profile blocks launch", () => {
    const form = { ...emptyAgentForm("harness"), name: "net-c", systemPrompt: "hi" };
    const opts = { knobIssues: [], byocUploading: false };
    expect(agentFormValid(form, CATALOGS, opts)).toBe(true);
    expect(agentFormValid({ ...form, networkVpc: true }, CATALOGS, opts)).toBe(false);
    expect(agentFormValid({ ...form, networkVpc: true, networkProfileId: "np-1" }, CATALOGS, opts)).toBe(true);
  });
});

describe("BYO mounts on every method (storage-mounts §3)", () => {
  const s3 = [{ arn: "arn:aws:s3files:us-east-1:1:ap/x", path: "/mnt/data" }];
  const efs = [{ arn: "arn:aws:elasticfilesystem:us-east-1:1:access-point/fsap-1", path: "/mnt/shared" }];
  const sent = {
    session_storage: { mount_path: "/mnt/workspace" },
    s3_files: [{ access_point_arn: s3[0].arn, mount_path: "/mnt/data" }],
    efs: [{ access_point_arn: efs[0].arn, mount_path: "/mnt/shared" }],
    mounts: [],
  };

  it("harness, zip_runtime, container, byoc and MCP servers send the same filesystem block", () => {
    for (const method of ["harness", "zip_runtime", "container"] as const) {
      const form = { ...emptyAgentForm(method), name: "fs-a", systemPrompt: "hi", s3Mounts: s3, efsMounts: efs, networkProfileId: "np-1" };
      expect(buildAgentSpec(form, CATALOGS).filesystem).toEqual(sent);
    }
    const byoc = { ...emptyAgentForm("byoc"), name: "fs-b", byocUploadId: "u1", s3Mounts: s3, efsMounts: efs, networkProfileId: "np-1" };
    expect(buildAgentSpec(byoc, CATALOGS).filesystem).toEqual(sent);
    expect(buildMcpServerSpec({ ...byoc, protocol: "mcp" as const, mcpTargetName: "crm" }).filesystem).toEqual(sent);
  });

  it("a mount without a profile needs one; only a container may use inline subnets", () => {
    for (const method of ["harness", "zip_runtime", "byoc"] as const) {
      const form = { ...emptyAgentForm(method), s3Mounts: s3, vpcSubnets: "subnet-1", vpcSgs: "sg-1" };
      expect(filesystemIssues(form).vpc).toBe(true);
      expect(filesystemIssues({ ...form, networkProfileId: "np-1" }).vpc).toBe(false);
    }
    const container = { ...emptyAgentForm("container"), s3Mounts: s3 };
    expect(filesystemIssues(container).vpc).toBe(true);
    expect(filesystemIssues({ ...container, vpcSubnets: "subnet-1", vpcSgs: "sg-1" }).vpc).toBe(false);
  });
});

describe("session storage and the S3 output folder are independent (task 11-27-session-fs-both)", () => {
  const base = { ...emptyAgentForm("zip_runtime"), name: "out-a", systemPrompt: "hi" };
  const both = { ...base, outputsFs: true };

  it("defaults to managed session storage with the folder off", () => {
    expect(emptyAgentForm("zip_runtime")).toMatchObject({ sessionFs: true, outputsFs: false });
  });

  it("serializes each toggle on its own, and both together", () => {
    const managed = buildAgentSpec(base, CATALOGS).filesystem;
    expect(managed?.session_storage).toEqual({ mount_path: "/mnt/workspace" });
    expect(managed).not.toHaveProperty("outputs");
    const bucket = buildAgentSpec({ ...base, sessionFs: false, outputsFs: true, outputsMount: "/mnt/out" }, CATALOGS).filesystem;
    expect(bucket?.outputs).toEqual({ mount_path: "/mnt/out" });
    expect(bucket).toHaveProperty("session_storage", null);
    const bothSent = buildAgentSpec({ ...both, outputsMount: "/mnt/out" }, CATALOGS).filesystem;
    expect(bothSent?.session_storage).toEqual({ mount_path: "/mnt/workspace" });
    expect(bothSent?.outputs).toEqual({ mount_path: "/mnt/out" });
    const none = buildAgentSpec({ ...base, sessionFs: false }, CATALOGS).filesystem;
    expect(none).toHaveProperty("session_storage", null);
    expect(none).not.toHaveProperty("outputs");
  });

  it("never sends the folder from the Harness", () => {
    expect(buildAgentSpec({ ...both, method: "harness" }, CATALOGS).filesystem).not.toHaveProperty("outputs");
  });

  it("round-trips a stored spec that carries both (the server's stamp is not echoed back)", () => {
    const stored = {
      filesystem: {
        session_storage: { mount_path: "/mnt/ws" },
        outputs: { mount_path: "/mnt/out", bucket: "b", project_key: "p" },
      },
    };
    const form = formFromStoredSpec("zip_runtime", "out-b", stored).form;
    expect(form).toMatchObject({ sessionFs: true, sessionMount: "/mnt/ws", outputsFs: true, outputsMount: "/mnt/out" });
    expect(buildAgentSpec(form, CATALOGS).filesystem).toMatchObject({
      session_storage: { mount_path: "/mnt/ws" },
      outputs: { mount_path: "/mnt/out" },
    });
    expect(buildAgentSpec(form, CATALOGS).filesystem?.outputs).toEqual({ mount_path: "/mnt/out" });
  });

  it("accepts both stores, and blocks the folder on a V1 Runtime", () => {
    expect(filesystemValid(both)).toBe(true);
    const v1 = { ...both, runtimeConfig: { ...both.runtimeConfig!, platform_version: "V1" as const } };
    expect(filesystemIssues(v1).outputsNeedsV2).toBe(true);
    expect(filesystemValid(v1)).toBe(false);
    // the Harness never carries the folder, so V1 does not matter there
    expect(filesystemIssues({ ...v1, method: "harness" }).outputsNeedsV2).toBe(false);
  });

  it("checks the path, the unique paths and the five-entry limit", () => {
    expect(filesystemIssues({ ...both, outputsMount: "/data" }).outputsMount).toBe(true);
    expect(filesystemIssues({ ...both, outputsMount: "/mnt/workspace" }).duplicatePaths).toBe(true);
    expect(filesystemIssues(both)).toMatchObject({ outputsMount: false, duplicatePaths: false, tooMany: false });
    // each store takes one of the five entries
    const rows = ["a", "b", "c", "d", "e"].map((x) => ({ storageId: `cat-${x}`, kind: "s3bucket" as const, mountPath: `/mnt/${x}`, mode: "ro" as const }));
    expect(storeCount(both)).toBe(2);
    expect(filesystemIssues({ ...both, catalogMounts: rows.slice(0, 3) as never }).tooMany).toBe(false);
    expect(filesystemIssues({ ...both, catalogMounts: rows.slice(0, 4) as never }).tooMany).toBe(true);
    // one store leaves four, no store leaves five, the Harness counts no folder
    expect(filesystemIssues({ ...base, catalogMounts: rows.slice(0, 4) as never }).tooMany).toBe(false);
    expect(filesystemIssues({ ...base, sessionFs: false, catalogMounts: rows as never }).tooMany).toBe(false);
    expect(storeCount({ ...both, method: "harness" })).toBe(1);
  });

  it("gates a BYOC agent on the filesystem too", () => {
    const byoc = { ...emptyAgentForm("byoc"), name: "fs-by", byocUploadId: "u1" };
    const opts = { knobIssues: [], byocUploading: false };
    expect(agentFormValid(byoc, CATALOGS, opts)).toBe(true);
    expect(agentFormValid({ ...byoc, outputsFs: true, outputsMount: "/data" }, CATALOGS, opts)).toBe(false);
  });
});

describe("storage catalog mounts in the wizard (storage-mounts SM-2)", () => {
  const picks = [
    { storageId: "datasets", path: "/mnt/datasets", mode: "ro" as const },
    { storageId: "state", path: "/mnt/state", mode: "rw" as const },
  ];

  it("starts empty, sends `filesystem.mounts` and round-trips through a stored spec", () => {
    expect(emptyAgentForm("harness").catalogMounts).toEqual([]);
    const form = { ...emptyAgentForm("harness"), name: "fs-c", systemPrompt: "hi", catalogMounts: picks, networkProfileId: "np-1" };
    const spec = buildAgentSpec(form, CATALOGS);
    expect(spec.filesystem?.mounts).toEqual([
      { storage_id: "datasets", mount_path: "/mnt/datasets", mode: "ro" },
      { storage_id: "state", mount_path: "/mnt/state", mode: "rw" },
    ]);
    expect(formFromStoredSpec("harness", "fs-c", spec).form.catalogMounts).toEqual(picks);
  });

  it("counts catalog picks as BYO mounts: a profile is needed", () => {
    const form = { ...emptyAgentForm("zip_runtime"), catalogMounts: picks };
    expect(hasByoMounts(form)).toBe(true);
    expect(filesystemIssues(form).vpc).toBe(true);
    expect(filesystemIssues({ ...form, networkProfileId: "np-1" }).vpc).toBe(false);
  });

  it("checks catalog paths for duplicates and caps inline + catalog at four", () => {
    const base = { ...emptyAgentForm("container"), networkProfileId: "np-1" };
    const dup = { ...base, s3Mounts: [{ arn: "arn:aws:s3files:us-east-1:1:ap/x", path: "/mnt/datasets" }], catalogMounts: picks };
    expect(filesystemIssues(dup).duplicatePaths).toBe(true);
    const inline = [1, 2, 3].map((i) => ({ arn: `arn:aws:s3files:us-east-1:1:ap/${i}`, path: `/mnt/in${i}` }));
    expect(filesystemIssues({ ...base, s3Mounts: inline.slice(0, 2), catalogMounts: picks }).tooMany).toBe(false);
    expect(filesystemIssues({ ...base, s3Mounts: inline, catalogMounts: picks }).tooMany).toBe(true);
  });
});

describe("context layer access mode (via)", () => {
  const NS = "dc09046b-28af-4c7b-a03a-4fa63cda243b";
  const harness = (contextLayer: ReturnType<typeof emptyAgentForm>["contextLayer"]) => ({
    ...emptyAgentForm("harness"),
    name: "hr-agent",
    contextLayer,
  });

  it("sends via only for gateway mode, the shape the backend stores", () => {
    const direct = harness({ namespaceId: NS, namespaceName: "AnyCompany HR" });
    expect(contextLayerSpec(direct)).toEqual({ context_layer: { namespace_id: NS, namespace_name: "AnyCompany HR" } });
    const explicit = harness({ namespaceId: NS, namespaceName: "", via: "direct" });
    expect(contextLayerSpec(explicit)).toEqual({ context_layer: { namespace_id: NS } });
    const gateway = harness({ namespaceId: NS, namespaceName: "HR", via: "gateway" });
    expect(contextLayerSpec(gateway)).toEqual({ context_layer: { namespace_id: NS, namespace_name: "HR", via: "gateway" } });
    expect(buildAgentSpec(gateway, CATALOGS).context_layer).toEqual({ namespace_id: NS, namespace_name: "HR", via: "gateway" });
  });

  it("keeps the old shapes: not owned, none, or a method that cannot carry a layer", () => {
    expect(contextLayerSpec({ ...harness(null), contextLayer: undefined })).toEqual({});
    expect(contextLayerSpec(harness(null))).toEqual({ context_layer: null });
    const container = { ...harness({ namespaceId: NS, namespaceName: "", via: "gateway" }), method: "container" as const };
    expect(contextLayerSpec(container)).toEqual({ context_layer: null });
  });

  it("reads the mode back from a stored spec", () => {
    const stored = { name: "hr-agent", method: "harness", system_prompt: "help" };
    const gateway = formFromStoredSpec("harness", "hr-agent", {
      ...stored,
      context_layer: { namespace_id: NS, namespace_name: "HR", via: "gateway" },
    }).form;
    expect(gateway.contextLayer).toEqual({ namespaceId: NS, namespaceName: "HR", via: "gateway" });
    expect(contextLayerSpec(gateway).context_layer).toEqual({ namespace_id: NS, namespace_name: "HR", via: "gateway" });
    const direct = formFromStoredSpec("harness", "hr-agent", { ...stored, context_layer: { namespace_id: NS } }).form;
    expect(direct.contextLayer).toEqual({ namespaceId: NS, namespaceName: "" });
    expect(contextLayerVia(direct.contextLayer)).toBe("direct");
  });

  it("switches the mode without touching the namespace", () => {
    const choice = { namespaceId: NS, namespaceName: "HR" };
    expect(withContextVia(choice, "gateway")).toEqual({ ...choice, via: "gateway" });
    expect(withContextVia({ ...choice, via: "gateway" }, "direct")).toEqual(choice);
    expect(withContextVia(null, "gateway")).toBeNull();
    expect(contextLayerVia(null)).toBe("direct");
  });
});

describe("managed COA gateway shown in the Gateway picker", () => {
  const NS = "dc09046b-28af-4c7b-a03a-4fa63cda243b";
  const form = (method: "harness" | "zip_runtime", via?: "direct" | "gateway", namespaceId = NS) => ({
    ...emptyAgentForm(method),
    contextLayer: { namespaceId, namespaceName: "AnyCompany HR", ...(via ? { via } : {}) },
  });

  it("is attached only for gateway mode with a namespace on a method that carries a layer", () => {
    expect(managedGatewayAttached(form("harness", "gateway"))).toBe(true);
    expect(managedGatewayAttached(form("zip_runtime", "gateway"))).toBe(true);
    expect(managedGatewayAttached(form("harness", "direct"))).toBe(false);
    expect(managedGatewayAttached(form("harness"))).toBe(false);
    expect(managedGatewayAttached(form("harness", "gateway", "  "))).toBe(false);
    expect(managedGatewayAttached({ ...form("zip_runtime", "gateway"), protocol: "a2a" })).toBe(false);
    expect(managedGatewayAttached({ ...emptyAgentForm("harness"), contextLayer: null })).toBe(false);
  });
});

describe("byoc re-publish", () => {
  const STORED_BYOC = {
    name: "byoc-agent",
    method: "byoc",
    model_id: "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    byoc: { artifact_kind: "code_zip", upload_id: "u1" },
    tools: [{ type: "builtin", name: "browser" }, CRM],
  };
  const keys = (tools: { type: string; name: string }[] | undefined) =>
    (tools ?? []).map((tool) => `${tool.type}:${tool.name}`);

  it("re-states the stored builtins via the form (edit prefill → same grant)", () => {
    const form = formFromStoredSpec("byoc", "byoc-agent", STORED_BYOC).form;
    expect(form.byocBuiltins).toEqual(["browser"]);
    const spec = republishSpec(buildAgentSpec(form, CATALOGS), STORED_BYOC);
    const byKey = Object.fromEntries((spec.tools ?? []).map((tool) => [`${tool.type}:${tool.name}`, tool]));
    expect(Object.keys(byKey).sort()).toEqual(["builtin:browser", "rest:crm"]);
    expect(byKey["builtin:browser"]).toEqual({ type: "builtin", name: "browser" });
    expect(byKey["rest:crm"].auth).toMatchObject({ connection: "team-idp", kind: "oauth2" });
  });

  it("adds a builtin the member checked on edit", () => {
    const stored = { ...STORED_BYOC, tools: [CRM] };
    const form = formFromStoredSpec("byoc", "byoc-agent", stored).form;
    expect(form.byocBuiltins).toEqual([]);
    const spec = republishSpec(buildAgentSpec({ ...form, byocBuiltins: ["code-interpreter", "browser"] }, CATALOGS), stored);
    expect(keys(spec.tools)).toEqual(["builtin:browser", "builtin:code-interpreter", "rest:crm"]);
  });

  it("drops a builtin the member unchecked (the form is the only source of truth)", () => {
    const form = formFromStoredSpec("byoc", "byoc-agent", STORED_BYOC).form;
    const spec = republishSpec(buildAgentSpec({ ...form, byocBuiltins: [] }, CATALOGS), STORED_BYOC);
    expect(keys(spec.tools)).toEqual(["rest:crm"]);
  });
});

describe("byoc builtin tool permissions", () => {
  const byocForm = () => ({ ...emptyAgentForm("byoc"), name: "byoc-agent", byocUploadId: "u1" });

  it("emits a bare builtin ToolRef per checked box (no config, no auth), in catalog order", () => {
    const spec = buildAgentSpec({ ...byocForm(), byocBuiltins: ["code-interpreter", "browser"] }, CATALOGS);
    expect(spec.tools).toEqual([
      { type: "builtin", name: "browser" },
      { type: "builtin", name: "code-interpreter" },
    ]);
  });

  it("puts the builtins ahead of the Identity rows", () => {
    const form = formFromStoredSpec("byoc", "byoc-agent", {
      method: "byoc",
      byoc: { artifact_kind: "code_zip", upload_id: "u1" },
      tools: [CRM],
    }).form;
    const spec = buildAgentSpec({ ...form, byocBuiltins: ["browser"] }, CATALOGS);
    expect((spec.tools ?? []).map((tool) => `${tool.type}:${tool.name}`)).toEqual(["builtin:browser", "rest:crm"]);
  });

  it("sends no tools at all when nothing is checked", () => {
    expect(buildAgentSpec(byocForm(), CATALOGS).tools).toBeUndefined();
  });

  it("does not leak into another method's spec after a method switch", () => {
    const checked = { ...byocForm(), byocBuiltins: ["browser", "code-interpreter"] as const };
    for (const method of ["harness", "zip_runtime", "container"] as const) {
      const spec = buildAgentSpec({ ...checked, byocBuiltins: [...checked.byocBuiltins], method }, CATALOGS);
      expect((spec.tools ?? []).filter((tool) => tool.type === "builtin")).toEqual([]);
    }
  });

  it("refills the boxes from a stored byoc spec, ignoring unknown builtin names", () => {
    const stored = {
      method: "byoc",
      byoc: { artifact_kind: "code_zip", upload_id: "u1" },
      tools: [
        { type: "builtin", name: "code-interpreter" },
        { type: "builtin", name: "browser" },
        { type: "builtin", name: "web-search" },
      ],
    };
    expect(formFromStoredSpec("byoc", "byoc-agent", stored).form.byocBuiltins).toEqual(["browser", "code-interpreter"]);
    expect(formFromStoredSpec("zip_runtime", "z", { ...stored, method: "zip_runtime" }).form.byocBuiltins).toEqual([]);
  });
});

import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import type { useAgentActions } from "../../../lib/agent-admin/actions";
import { agentPermissions } from "../../../lib/agent-admin/permissions";
import type { AgentInfo } from "../../../lib/api";
import { MemoryRouter } from "react-router-dom";

import { ActionDialog } from "./ActionDialog";
import { BasicInfoCard } from "./BasicInfoCard";
import { HeaderActions } from "./HeaderActions";
import { moreEntries } from "./menus";
import { AGENT_TABS, detailSections, parseTab, tabSearch } from "./tabs";

const agent = (over: Partial<AgentInfo> = {}): AgentInfo =>
  ({
    id: "a1",
    name: "lab-service",
    display_name: "Lab service",
    method: "zip_runtime",
    status: "active",
    arn: "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/lab-service-x",
    resource_id: null,
    version: "3",
    owner: "alice",
    error: null,
    spec: {},
    system: null,
    invoke_capability: { eligible: true, reason: null, reason_code: null },
    created_at: null,
    updated_at: null,
    deployments: [{ id: "d", agent_id: "a1", job_id: "j", status: "succeeded", stages: [], started_at: null, ended_at: null }],
    ...over,
  }) as AgentInfo;

const PRESET = { managed: true as const, key: "assistant", label: "Assistant", skill_version: "1", protected_actions: [] };
const member = agentPermissions({ canDeploy: true, canDelete: true, canConvert: true, isAdmin: false, locked: false, lockTitle: undefined });

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("tabs in the URL", () => {
  it("parses a known tab and falls back to the overview", () => {
    expect(AGENT_TABS).toEqual(["overview", "deploy", "access", "release"]);
    expect(parseTab("access")).toBe("access");
    expect(parseTab("nope")).toBe("overview");
    expect(parseTab(null)).toBe("overview");
  });

  it("keeps the other params and drops tab= for the overview", () => {
    const params = new URLSearchParams("id=a1&q=lab");
    expect(tabSearch(params, "access").toString()).toBe("id=a1&q=lab&tab=access");
    expect(tabSearch(new URLSearchParams("id=a1&tab=deploy"), "overview").toString()).toBe("id=a1");
  });
});

describe("detailSections per method", () => {
  it("zip / container / BYOC / Studio runtimes show the full set", () => {
    for (const method of ["zip_runtime", "container", "byoc", "studio"] as const) {
      const s = detailSections(agent({ method }));
      expect(s, method).toMatchObject({ lifecycle: true, tryIt: true, snapshots: true, versions: true, inbound: true, identity: true, gate: true, promotion: true });
    }
  });

  it("a Harness agent has no inbound switch (no JWT authorizer)", () => {
    expect(detailSections(agent({ method: "harness" })).inbound).toBe(false);
  });

  it("an imported runtime has no snapshots, versions card, inbound switch or identity page", () => {
    const s = detailSections(agent({ method: "discovered_runtime" }));
    expect(s).toMatchObject({ snapshots: false, versions: false, inbound: false, identity: false });
  });

  it("an MCP server keeps its panel, without try-it or the gate", () => {
    const s = detailSections(agent({ spec: { protocol: "mcp" } }));
    expect(s).toMatchObject({ mcpPanel: true, lifecycle: false, tryIt: false, gate: false, inbound: false });
  });

  it("a system preset shows its note and is never promoted", () => {
    const s = detailSections(agent({ system: PRESET }));
    expect(s).toMatchObject({ systemNote: true, promotion: false });
  });

  it("BYOC and converted cards follow the spec", () => {
    expect(detailSections(agent({ method: "byoc", spec: { byoc: { artifact_kind: "zip" } } })).byoc).toBe(true);
    expect(detailSections(agent({ spec: { source_harness: { agent_name: "h" } } })).converted).toBe(true);
    expect(detailSections(agent()).byoc).toBe(false);
  });

  it("while deploying the versions card waits; no invoke means no integration", () => {
    expect(detailSections(agent({ status: "deploying" })).versions).toBe(false);
    expect(detailSections(agent({ invoke_capability: { eligible: false, reason: "x", reason_code: null } })).integration).toBe(false);
  });
});

describe("⋯ More", () => {
  it("convert for an active Harness agent, roll back, the classic page, delete last", () => {
    expect(moreEntries(agent({ method: "harness" }), member).map((e) => e.key)).toEqual(["convert", "rollback", "classic", "delete"]);
    expect(moreEntries(agent(), member).map((e) => e.key)).toEqual(["rollback", "classic", "delete"]);
  });

  it("an import is removed, never rolled back; a preset is neither deleted nor converted", () => {
    expect(moreEntries(agent({ method: "discovered_runtime" }), member).map((e) => e.key)).toEqual(["classic", "remove"]);
    expect(moreEntries(agent({ system: PRESET, method: "harness" }), member).map((e) => e.key)).toEqual(["classic"]);
  });

  it("no rollback while deploying; the prod lock disables rather than hides, but never the classic page", () => {
    expect(moreEntries(agent({ status: "deploying" }), member).map((e) => e.key)).toEqual(["classic", "delete"]);
    const locked = agentPermissions({ canDeploy: true, canDelete: true, canConvert: true, isAdmin: false, locked: true, lockTitle: "prod" });
    const entries = moreEntries(agent(), locked);
    expect(entries.filter((e) => e.key !== "classic").every((e) => e.disabled && e.why === "lock")).toBe(true);
    expect(entries.find((e) => e.key === "classic")).toMatchObject({ disabled: false, why: null });
  });

  it("hides what the role may not do; the classic page stays", () => {
    const viewer = agentPermissions({ canDeploy: false, canDelete: false, canConvert: false, isAdmin: false, locked: false, lockTitle: undefined });
    expect(moreEntries(agent({ method: "harness" }), viewer).map((e) => e.key)).toEqual(["classic"]);
  });
});

describe("ActionDialog", () => {
  const actions = (kind: "delete" | "convert", a: AgentInfo, error: unknown = null) =>
    ({ pending: { kind, agent: a }, busy: false, error, askDelete: vi.fn(), askConvert: vi.fn(), cancel: vi.fn(), confirm: vi.fn() }) as unknown as ReturnType<typeof useAgentActions>;

  it("deleting asks for the resource name, the confirm disabled until typed", () => {
    const html = renderToStaticMarkup(<ActionDialog actions={actions("delete", agent())} />);
    expect(html).toContain('data-testid="v3-agent-delete-dialog"');
    expect(html).toContain('data-testid="v3-agent-delete-name"');
    expect(html).toContain("Type lab-service to confirm");
    expect(html).toMatch(/<button[^>]*class="v3-btn danger"[^>]*disabled/);
  });

  it("removing an import says the runtime stays and asks for no name", () => {
    const html = renderToStaticMarkup(<ActionDialog actions={actions("delete", agent({ method: "discovered_runtime" }))} />);
    expect(html).toContain('data-testid="v3-agent-remove-dialog"');
    expect(html).not.toContain("v3-agent-delete-name");
    expect(html).toContain("the runtime on AWS is kept");
    expect(html).not.toMatch(/<button[^>]*class="v3-btn danger"[^>]*disabled/);
  });

  it("converting needs no name; a failure shows the error detail", () => {
    const html = renderToStaticMarkup(<ActionDialog actions={actions("convert", agent({ method: "harness" }), new Error("nope"))} />);
    expect(html).toContain('data-testid="v3-agent-convert-dialog"');
    expect(html).not.toContain("v3-agent-delete-name");
    expect(html).toContain('data-testid="v3-agent-action-error"');
    expect(html).toContain("nope");
    // the twin is a NEW agent: the dialog asks for its project, confirm waits for it (#356)
    expect(html).toContain('data-testid="v3-convert-project-field"');
    expect(html).toMatch(/<button[^>]*class="v3-btn primary"[^>]*disabled/);
  });

  it("renders nothing without a pending action", () => {
    const idle = { ...actions("delete", agent()), pending: null } as ReturnType<typeof useAgentActions>;
    expect(renderToStaticMarkup(<ActionDialog actions={idle} />)).toBe("");
  });
});

describe("header and basic information", () => {
  // the router's links use useLayoutEffect, which the static renderer warns about
  beforeAll(() => {
    const error = console.error.bind(console);
    vi.spyOn(console, "error").mockImplementation((...args: unknown[]) => {
      if (String(args[0]).includes("useLayoutEffect does nothing on the server")) return;
      error(...args);
    });
  });
  const idle = { pending: null, busy: false, error: null, askDelete: vi.fn(), askConvert: vi.fn(), cancel: vi.fn(), confirm: vi.fn() } as unknown as ReturnType<typeof useAgentActions>;
  const header = (a: AgentInfo, perms = member) =>
    renderToStaticMarkup(
      <MemoryRouter>
        <HeaderActions agent={a} perms={perms} actions={idle} refreshing={false} onRefresh={vi.fn()} onRollback={vi.fn()} />
      </MemoryRouter>,
    );

  it("links chat, gate, observability and the identity page; Edit configuration ▾ and More are menus", () => {
    const html = header(agent());
    expect(html).toContain("Edit configuration");
    expect(html).toContain('title="Change the configuration and publish a new version"');
    expect(html).not.toMatch(/Manage/);
    expect(html).toContain('href="/v3/chat?agent=a1"');
    expect(html).toContain('href="/v3/gate?agent=a1"');
    expect(html).toContain('href="/v3/observability"');
    expect(html).toContain('href="/v2/agents?view=identity&amp;id=a1"');
    expect(html).toContain('data-testid="v3-agent-edit"');
    expect(html).toContain('data-testid="v3-agent-more"');
    expect(html).not.toContain("/v2/agents?view=detail");
  });

  it("an import has no identity link; an MCP server no chat or gate; a viewer no Edit configuration", () => {
    expect(header(agent({ method: "discovered_runtime" }))).not.toContain("v3-agent-identity-open");
    const mcp = header(agent({ spec: { protocol: "mcp" } }));
    expect(mcp).not.toContain("/v3/chat");
    expect(mcp).not.toContain("/v3/gate");
    const viewer = agentPermissions({ canDeploy: false, canDelete: false, canConvert: false, isAdmin: false, locked: false, lockTitle: undefined });
    const html = header(agent(), viewer);
    expect(html).not.toContain('data-testid="v3-agent-edit"');
    // More still opens the classic detail page
    expect(html).toContain('data-testid="v3-agent-more"');
  });

  it("the Chinese wording: 编辑配置 with its hint, never 管理", async () => {
    await i18n.changeLanguage("zh-CN");
    try {
      const html = header(agent());
      expect(html).toContain("编辑配置");
      expect(html).toContain('title="修改配置并发布新版本"');
      expect(html).not.toContain("管理");
    } finally {
      await i18n.changeLanguage("en");
    }
  });

  it("lists the agent's facts with copy buttons", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <BasicInfoCard agent={agent({ registry_record_id: "rec-1", spec: { protocol: "a2a" } })} />
      </MemoryRouter>,
    );
    expect(html).toContain("Lab service");
    expect(html).toContain("lab-service");
    expect(html).toContain("A2A");
    expect(html).toContain('href="/v3/registry?id=rec-1"');
    expect(html).toContain('aria-label="Copy ARN"');
  });
});

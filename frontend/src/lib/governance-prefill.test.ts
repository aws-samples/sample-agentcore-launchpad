import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  buildAllowlistStatement,
  buildForbidToolForMembers,
  buildPermitUsersCallTools,
  buildPermitWorkspaceAgentsCallTools,
  canonicalCedar,
  isToolPattern,
  parseArgumentsObject,
  patternExampleName,
  POLICY_NAME_RE,
  toolPolicyPrefill,
} from "./governance";

/** The server templates the console builders must mirror (D20-obs2; Global parity, contract
 *  amendment 9) — read from the backend tree when present (the templates land with the
 *  schema / compiler branch; until then the inline canonical form below is the pin). */
const serverTemplate = (name: string): string | null => {
  const path = fileURLToPath(new URL(`../../../backend/app/policy/templates/${name}.cedar`, import.meta.url));
  return existsSync(path) ? readFileSync(path, "utf-8") : null;
};
const workspaceAgentsTemplate = serverTemplate("permit_workspace_agents_call_tools");
const usersTemplate = serverTemplate("permit_users_call_tools");
// the canonical forms of the two .cedar files (comment lines dropped)
const WORKSPACE_AGENTS_CANONICAL = `permit(
  principal in Launchpad::Workspace::"__WORKSPACE_ID__",
  action == Launchpad::Action::"call_tool",
  resource
);`;
const USERS_CANONICAL = `permit(
  principal in Launchpad::Role::"member",
  action == Launchpad::Action::"call_tool",
  resource
);

permit(
  principal in Launchpad::Role::"admin",
  action == Launchpad::Action::"call_tool",
  resource
);

permit(
  principal is Launchpad::System,
  action == Launchpad::Action::"call_tool",
  resource
);

permit(
  principal is Launchpad::ApiKey,
  action == Launchpad::Action::"call_tool",
  resource
);`;

const tools = [
  { name: "crm___getCustomer", target: "crm" },
  { name: "crm___listOrders", target: "crm" },
  { name: "wiki___search", target: "wiki" },
];

describe("toolPolicyPrefill", () => {
  it("picks exactly the target's tools and a valid policy name", () => {
    expect(toolPolicyPrefill(tools, "crm")).toEqual({
      name: "allow_crm",
      tools: ["crm___getCustomer", "crm___listOrders"],
    });
  });

  it("slugs target names the store refuses and caps the length", () => {
    const { name, tools: picked } = toolPolicyPrefill(tools, `my-target-${"x".repeat(60)}`);
    expect(name).toMatch(/^allow_my_target_x+$/);
    expect(name.length).toBe(48);
    expect(POLICY_NAME_RE.test(name)).toBe(true);
    expect(picked).toEqual([]);
  });
});

// D20: statements use the self-hosted schema's `Launchpad::` namespace, never AgentCore::
describe("Cedar templates (Launchpad:: namespace)", () => {
  // Global parity (amendment 9): the Stage-A workspace baseline — every agent of the workspace
  // may call its declared tools; the console substitutes the workspace id (escaped)
  it("builds the workspace-agents call_tool baseline with the workspace id substituted", () => {
    const cedar = buildPermitWorkspaceAgentsCallTools("default");
    expect(cedar).toBe(WORKSPACE_AGENTS_CANONICAL.replace("__WORKSPACE_ID__", "default"));
    expect(cedar).toContain('action == Launchpad::Action::"call_tool"');
    expect(cedar).not.toContain("invoke");
    expect(cedar).not.toContain("AgentCore::");
    expect(buildPermitWorkspaceAgentsCallTools('ws"x')).toContain('Launchpad::Workspace::"ws\\"x"');
    // the server template, when the entities payload carries it, is used (canonical, substituted)
    expect(buildPermitWorkspaceAgentsCallTools("default", WORKSPACE_AGENTS_CANONICAL)).toBe(cedar);
    expect(buildPermitWorkspaceAgentsCallTools("default", "  ")).toBe(cedar);
    if (workspaceAgentsTemplate !== null) expect(canonicalCedar(workspaceAgentsTemplate)).toBe(WORKSPACE_AGENTS_CANONICAL);
  });
  // D20-obs2 still holds: a user has ONE Role parent — a member-only baseline default-denies
  // admins. The Stage-BT builder emits exactly the server template's four permits (member,
  // admin, `principal is System` for the eval / canary tokens, `principal is ApiKey` for the
  // `/v1` keys — lead ruling option D, Global M2M parity).
  it("emits the same four permits as the server template permit_users_call_tools.cedar", () => {
    const cedar = buildPermitUsersCallTools();
    expect(cedar).toContain('principal in Launchpad::Role::"member"');
    expect(cedar).toContain('principal in Launchpad::Role::"admin"');
    expect(cedar).toContain("principal is Launchpad::System");
    expect(cedar).toContain("principal is Launchpad::ApiKey");
    expect(cedar.match(/permit\(/g)).toHaveLength(4);
    expect(cedar).toBe(USERS_CANONICAL);
    expect(buildPermitUsersCallTools(USERS_CANONICAL)).toBe(cedar);
    expect(buildPermitUsersCallTools("  ")).toBe(cedar);
    if (usersTemplate !== null) expect(canonicalCedar(usersTemplate)).toBe(USERS_CANONICAL);
    expect(canonicalCedar("// note\npermit(principal, action, resource);\n")).toBe("permit(principal, action, resource);");
  });
  it("builds a tool allowlist, singular and plural, optionally per agent", () => {
    expect(buildAllowlistStatement(["crm___getCustomer"])).toContain('resource == Launchpad::Tool::"crm___getCustomer"');
    const many = buildAllowlistStatement(["a___x", "b___y"], "agt_1");
    expect(many).toContain('principal == Launchpad::Agent::"agt_1"');
    // a plural allowlist is a `when` set-membership clause — `resource in [...]` is not valid
    // in the policy scope (Cedar: "expected single entity uid … found set of entity uids")
    expect(many).toContain('when {\n  resource in [Launchpad::Tool::"a___x", Launchpad::Tool::"b___y"]');
    expect(many).not.toMatch(/,\s*resource in \[/);
    expect(many).toContain('action == Launchpad::Action::"call_tool"');
  });
  it("escapes quotes inside entity ids", () => {
    expect(buildAllowlistStatement(['we"ird'])).toContain('Launchpad::Tool::"we\\"ird"');
  });
  // P1-c3: an entities row may be an fnmatch PATTERN (`crm___*`, `pattern: true`) — the
  // prefill keeps it verbatim inside `resource.name like` (the backend template's clause)
  // and never builds an entity literal `Tool::"crm___*"` from a `*`-bearing name.
  it("emits `resource.name like` for a pattern row and an entity literal only for concrete rows", () => {
    const pattern = buildAllowlistStatement([{ name: "crm___*", pattern: true }], "agt_1");
    expect(pattern).toContain('principal == Launchpad::Agent::"agt_1"');
    expect(pattern).toContain('  resource\n) when {\n  resource.name like "crm___*"\n};');
    expect(pattern).not.toContain('Tool::"crm___*"');
    // the server flag wins, but a bare `*`-bearing id is still treated as a pattern
    expect(buildAllowlistStatement(["hr-database___*"])).toContain('resource.name like "hr-database___*"');
    expect(isToolPattern("crm___*")).toBe(true);
    expect(isToolPattern("crm___getCustomer")).toBe(false);
    // mixed: patterns become `like` clauses, the concrete ids stay one entity set
    const mixed = buildAllowlistStatement([
      { name: "crm___*", pattern: true },
      { name: "wiki___search", pattern: false },
      { name: "hr___get", pattern: false },
    ]);
    expect(mixed).toContain('resource.name like "crm___*" ||\n  resource in [Launchpad::Tool::"wiki___search", Launchpad::Tool::"hr___get"]');
    expect(mixed).not.toContain('Tool::"crm___*"');
    // a concrete-only list is unchanged: entity literal in the scope, no `when`
    expect(buildAllowlistStatement([{ name: "crm___getCustomer", pattern: false }])).not.toContain("when {");
  });
  it("forbids a pattern with `like` while keeping the admin escape hatch", () => {
    const cedar = buildForbidToolForMembers({ name: "hr-database___*", pattern: true });
    expect(cedar).toContain('  resource\n) when {\n  resource.name like "hr-database___*"\n} unless { principal in Launchpad::Role::"admin" };');
    expect(cedar).not.toContain('Tool::"hr-database___*"');
  });
  it("forbids a tool unless the principal is an admin", () => {
    const cedar = buildForbidToolForMembers("hr-database___create_payout");
    expect(cedar.startsWith("forbid(")).toBe(true);
    expect(cedar).toContain('unless { principal in Launchpad::Role::"admin" }');
  });
});

describe("patternExampleName (simulator pattern rows)", () => {
  it("derives a concrete id the pattern matches", () => {
    expect(patternExampleName("crm___*")).toBe("crm___example");
    expect(patternExampleName("crm___get?")).toBe("crm___getx");
    expect(patternExampleName("crm___[gs]et")).toBe("crm___get");
    expect(patternExampleName("crm___[!y]et")).toBe("crm___zet");
    // a concrete id passes through untouched
    expect(patternExampleName("crm___getCustomer")).toBe("crm___getCustomer");
  });
});

describe("parseArgumentsObject", () => {
  it("accepts an object, defaults empty text to {}", () => {
    expect(parseArgumentsObject(' {"amount": 5} ')).toEqual({ amount: 5 });
    expect(parseArgumentsObject("")).toEqual({});
  });
  it("rejects arrays, scalars and broken JSON", () => {
    expect(parseArgumentsObject("[1]")).toBeNull();
    expect(parseArgumentsObject("5")).toBeNull();
    expect(parseArgumentsObject("{")).toBeNull();
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type AgentInfo, type DeploymentInfo, type Workspace } from "../api";
import { needsTypedName, performAgentAction, typedNameMatches } from "./actions";
import { deployPhase } from "./deploy";
import { classicDetailPath, editKind, editPath, identityPath } from "./edit";
import { inboundFacts, inboundSwitchState } from "./inbound";
import { agentPermissions, type AgentPermissionInput } from "./permissions";
import { releasable, releaseTargets } from "./release";
import { comparePair, showValue, togglePick } from "./snapshots";

const agent = (over: Partial<AgentInfo> = {}): AgentInfo =>
  ({
    id: "a1",
    name: "lab-service",
    display_name: null,
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
    ...over,
  }) as AgentInfo;

const PRESET = { managed: true as const, key: "assistant", label: "Assistant", skill_version: "1", protected_actions: [] };

const input = (over: Partial<AgentPermissionInput> = {}): AgentPermissionInput => ({
  canDeploy: true,
  canDelete: true,
  canConvert: true,
  isAdmin: false,
  locked: false,
  lockTitle: undefined,
  ...over,
});

afterEach(() => vi.restoreAllMocks());

describe("agentPermissions — role × preset × prod lock × status", () => {
  const roles = {
    admin: input({ isAdmin: true }),
    member: input(),
    viewer: input({ canDeploy: false, canDelete: false, canConvert: false }),
  };

  it("edits when the role deploys, never while deploying, a preset only as an admin, never an import", () => {
    for (const [role, perms] of Object.entries(roles)) {
      const p = agentPermissions(perms);
      const deployer = role !== "viewer";
      expect(p.canEdit(agent()), role).toBe(deployer);
      expect(p.canEdit(agent({ status: "deploying" })), role).toBe(false);
      expect(p.canEdit(agent({ system: PRESET })), role).toBe(role === "admin");
      expect(p.canEdit(agent({ method: "discovered_runtime" })), role).toBe(false);
    }
  });

  it("never deletes or converts a preset; converts only an active Harness agent", () => {
    const p = agentPermissions(roles.admin);
    expect(p.canDelete(agent())).toBe(true);
    expect(p.canDelete(agent({ system: PRESET }))).toBe(false);
    expect(p.canDelete(agent({ method: "discovered_runtime" }))).toBe(true);
    expect(p.canConvert(agent({ method: "harness" }))).toBe(true);
    expect(p.canConvert(agent({ method: "harness", status: "failed" }))).toBe(false);
    expect(p.canConvert(agent({ method: "harness", system: PRESET }))).toBe(false);
    expect(p.canConvert(agent())).toBe(false);
    expect(agentPermissions(roles.viewer).canDelete(agent())).toBe(false);
  });

  it("the prod lock does not change who may act, only that the controls are disabled", () => {
    const p = agentPermissions(input({ locked: true, lockTitle: "prod" }));
    expect(p.locked).toBe(true);
    expect(p.lockTitle).toBe("prod");
    expect(p.canEdit(agent())).toBe(true);
    expect(p.canDelete(agent())).toBe(true);
  });

  it("editState: hidden without the permission or for an import; otherwise shown with the reason it is disabled", () => {
    expect(agentPermissions(roles.viewer).editState(agent())).toEqual({ visible: false, blocked: null });
    expect(agentPermissions(roles.member).editState(agent({ method: "discovered_runtime" }))).toEqual({ visible: false, blocked: null });
    expect(agentPermissions(roles.member).editState(agent({ system: PRESET }))).toEqual({ visible: true, blocked: "presetAdminOnly" });
    expect(agentPermissions(roles.admin).editState(agent({ system: PRESET }))).toEqual({ visible: true, blocked: null });
    expect(agentPermissions(roles.member).editState(agent({ status: "deploying" }))).toEqual({ visible: true, blocked: "deploying" });
    expect(agentPermissions(input({ locked: true })).editState(agent())).toEqual({ visible: true, blocked: "locked" });
    expect(agentPermissions(roles.member).editState(agent())).toEqual({ visible: true, blocked: null });
  });
});

describe("edit targets", () => {
  it("opens the canvas, the preset flow or the re-publish wizard", () => {
    expect(editKind(agent({ method: "studio" }))).toBe("canvas");
    expect(editPath(agent({ method: "studio" }))).toBe("/create/studio?agent=a1");
    expect(editKind(agent({ system: PRESET }))).toBe("preset");
    expect(editPath(agent({ system: PRESET }))).toBe("/agents/a1/edit");
    expect(editKind(agent({ method: "harness" }))).toBe("wizard");
    expect(editPath(agent({ id: "a b" }))).toBe("/v2/agents?view=edit&id=a%20b");
  });

  it("keeps the classic detail page and the identity page reachable", () => {
    expect(classicDetailPath("a1")).toBe("/v2/agents?view=detail&id=a1&full=1");
    expect(identityPath("a1")).toBe("/v2/agents?view=identity&id=a1");
  });
});

describe("delete / convert", () => {
  it("asks for the typed name to delete a platform agent, not to remove an import or to convert", () => {
    expect(needsTypedName("delete", agent())).toBe(true);
    expect(needsTypedName("delete", agent({ method: "discovered_runtime" }))).toBe(false);
    expect(needsTypedName("convert", agent({ method: "harness" }))).toBe(false);
  });

  it("matches the resource name exactly, spaces around it ignored", () => {
    const a = agent({ display_name: "Lab service" });
    expect(typedNameMatches(a, "lab-service")).toBe(true);
    expect(typedNameMatches(a, "  lab-service ")).toBe(true);
    expect(typedNameMatches(a, "Lab service")).toBe(false);
    expect(typedNameMatches(a, "lab-servic")).toBe(false);
  });

  it("deletes by id and converts into the new agent", async () => {
    const del = vi.spyOn(api, "deleteAgent").mockResolvedValue(undefined as never);
    expect(await performAgentAction("delete", agent())).toEqual({ kind: "delete", agent: agent() });
    expect(del).toHaveBeenCalledWith("a1");
    const twin = agent({ id: "a2", name: "lab-service-code" });
    const conv = vi.spyOn(api, "convertAgent").mockResolvedValue({ agent: twin, job_id: "j", deployment_id: "d" });
    expect(await performAgentAction("convert", agent({ method: "harness" }), "risk-bot")).toEqual({ kind: "convert", agent: twin });
    // the twin is filed under the project the dialog picked (#356)
    expect(conv).toHaveBeenCalledWith("a1", "risk-bot");
  });

  it("passes the API error through untouched", async () => {
    const boom = new Error("refused");
    vi.spyOn(api, "deleteAgent").mockRejectedValue(boom);
    await expect(performAgentAction("delete", agent())).rejects.toBe(boom);
  });
});

describe("snapshots", () => {
  it("picks at most two, the oldest pick dropping out", () => {
    expect(togglePick([], 3)).toEqual([3]);
    expect(togglePick([3], 5)).toEqual([3, 5]);
    expect(togglePick([3, 5], 7)).toEqual([5, 7]);
    expect(togglePick([3, 5], 3)).toEqual([5]);
  });

  it("compares exactly two, older first", () => {
    expect(comparePair([7, 2])).toEqual([2, 7]);
    expect(comparePair([7])).toBeNull();
  });

  it("shows diff values as text", () => {
    expect(showValue(null)).toBe("—");
    expect(showValue("")).toBe("—");
    expect(showValue("x")).toBe("x");
    expect(showValue({ a: 1 })).toBe('{\n  "a": 1\n}');
  });
});

describe("release", () => {
  it("releases only an active, non-preset agent", () => {
    expect(releasable(agent())).toBe(true);
    expect(releasable(agent({ status: "failed" }))).toBe(false);
    expect(releasable(agent({ system: PRESET }))).toBe(false);
  });

  it("targets every workspace but this one", () => {
    const ws = [{ id: "w1" }, { id: "w2" }] as Workspace[];
    expect(releaseTargets(ws, "w1").map((w) => w.id)).toEqual(["w2"]);
    expect(releaseTargets(ws, null)).toHaveLength(2);
  });
});

describe("deployPhase", () => {
  const dep = (over: Partial<DeploymentInfo>): DeploymentInfo => ({
    id: "d",
    agent_id: "a1",
    job_id: "j",
    status: "running",
    stages: [],
    started_at: "2026-10-14T10:00:00",
    ended_at: null,
    ...over,
  });
  const at = Date.parse("2026-10-14T10:01:05Z");

  it("runs the clock while in flight", () => {
    expect(deployPhase(dep({}), at)).toEqual({ failed: false, inFlight: true, succeeded: false, elapsed: "1m 05s" });
  });

  it("stops at the end, and a failed stage fails the deploy", () => {
    expect(deployPhase(dep({ status: "succeeded", ended_at: "2026-10-14T10:00:30" }), at).elapsed).toBe("30s");
    expect(deployPhase(dep({ status: "succeeded", ended_at: "2026-10-14T10:00:30" }), at).succeeded).toBe(true);
    const failed = deployPhase(dep({ stages: [{ name: "package", status: "failed", detail: "" }] as DeploymentInfo["stages"] }), at);
    expect(failed.failed).toBe(true);
    expect(failed.inFlight).toBe(false);
  });
});

describe("inbound", () => {
  const loaded = { data: { cognito_issuer: "https://cognito-idp.us-east-1.amazonaws.com/pool" }, loading: false, error: null };

  it("shows inbound auth for an own HTTP runtime of a JWT-capable method only", () => {
    expect(inboundFacts(agent()).shown).toBe(true);
    expect(inboundFacts(agent({ method: "harness" })).shown).toBe(false);
    expect(inboundFacts(agent({ method: "discovered_runtime" })).shown).toBe(false);
    expect(inboundFacts(agent({ arn: null })).shown).toBe(false);
    expect(inboundFacts(agent({ spec: { protocol: "mcp" } })).shown).toBe(false);
    expect(inboundFacts(agent({ spec: { inbound_auth: { mode: "jwt" } } })).pinned).toBe("jwt");
    expect(inboundFacts(agent()).mode).toBe("iam");
  });

  it("switching to JWT waits for the defaults; nothing switches while deploying", () => {
    expect(inboundSwitchState({ mode: "iam", jwtDiscoveryUrl: null, busy: false, defaults: loaded })).toMatchObject({ target: "jwt", switchBlocked: false });
    expect(inboundSwitchState({ mode: "iam", jwtDiscoveryUrl: null, busy: true, defaults: loaded }).switchBlocked).toBe(true);
    const failed = inboundSwitchState({ mode: "iam", jwtDiscoveryUrl: null, busy: false, defaults: { data: null, loading: false, error: "x" } });
    expect(failed).toMatchObject({ switchBlocked: true, defaultsFailed: true });
    expect(inboundSwitchState({ mode: "jwt", jwtDiscoveryUrl: null, busy: false, defaults: { data: null, loading: true, error: null } })).toMatchObject({ target: "iam", switchBlocked: false, defaultsFailed: false });
  });

  it("warns when a JWT agent trusts an issuer other than the workspace pool", () => {
    const foreign = inboundSwitchState({ mode: "jwt", jwtDiscoveryUrl: "https://idp.example.com/.well-known/openid-configuration", busy: false, defaults: loaded });
    expect(foreign.mismatch).toEqual({ agentIssuer: "https://idp.example.com", workspaceIssuer: "https://cognito-idp.us-east-1.amazonaws.com/pool" });
    const own = inboundSwitchState({ mode: "jwt", jwtDiscoveryUrl: "https://cognito-idp.us-east-1.amazonaws.com/pool/.well-known/openid-configuration", busy: false, defaults: loaded });
    expect(own.mismatch).toBeNull();
  });
});

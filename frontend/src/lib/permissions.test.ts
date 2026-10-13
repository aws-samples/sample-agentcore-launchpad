import { describe, expect, it } from "vitest";

import { AGENT_PERMISSIONS, togglePermission, type AgentPermission } from "./api";

// Effective map as GET /api/users returns it for a member with every default grant.
const member = Object.fromEntries(AGENT_PERMISSIONS.map((k) => [k, true])) as Record<AgentPermission, boolean>;

describe("togglePermission", () => {
  it("sends every key, so an earlier denial survives the next click", () => {
    const afterFirst = togglePermission(member, "agents.delete", false);
    const afterSecond = togglePermission(afterFirst, "eval.run", false);
    expect(Object.keys(afterSecond).sort()).toEqual([...AGENT_PERMISSIONS].sort());
    expect(afterSecond["agents.delete"]).toBe(false);
    expect(afterSecond["eval.run"]).toBe(false);
  });

  it("keeps a revoked permission revoked when another chip is granted", () => {
    const denied = { ...member, "agents.deploy": false, "memory.manage": false };
    const next = togglePermission(denied, "memory.manage", true);
    expect(next["agents.deploy"]).toBe(false);
    expect(next["memory.manage"]).toBe(true);
  });

  it("treats a missing key the way the chips display it (granted)", () => {
    expect(togglePermission({}, "eval.run", false)["agents.deploy"]).toBe(true);
    expect(togglePermission(null, "eval.run", false)["eval.run"]).toBe(false);
  });
});

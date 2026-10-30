import { describe, expect, it } from "vitest";

import type { WorkspaceRoleTemplate } from "../lib/api";
import { deployedLabel, roleTemplateSummary } from "./use-role-template";

/** Echoes the key and its params, so the assertions read what the copy is given. */
const t = (key: string, opts?: Record<string, unknown>) =>
  opts ? `${key.replace("v2.workspaces.roleTemplate.", "")}:${JSON.stringify(opts)}` : key.replace("v2.workspaces.roleTemplate.", "");

function verdict(patch: Partial<WorkspaceRoleTemplate>): WorkspaceRoleTemplate {
  return {
    workspace_id: "spoke-use2",
    status: "current",
    deployed: 1,
    expected: 1,
    variant: "workspace",
    template: "infra/spoke/launchpad-workspace-role.yaml",
    stack_command: "aws cloudformation deploy …",
    ...patch,
  };
}

describe("roleTemplateSummary", () => {
  it("says checking while the first read is in flight, and nothing once it failed", () => {
    expect(roleTemplateSummary(t, null, true)).toBe("checking");
    expect(roleTemplateSummary(t, null, false)).toBe("—");
  });

  it("names the deployed and expected versions", () => {
    expect(roleTemplateSummary(t, verdict({}), false)).toBe('current:{"deployed":1}');
    expect(roleTemplateSummary(t, verdict({ status: "outdated", deployed: 0, expected: 2 }), false)).toBe(
      'outdatedShort:{"deployed":"v0","expected":2}',
    );
    expect(roleTemplateSummary(t, verdict({ status: "newer", deployed: 3 }), false)).toBe(
      'newer:{"deployed":3,"expected":1}',
    );
    expect(roleTemplateSummary(t, verdict({ status: "unknown", deployed: null, reason: "ServiceFailure" }), false)).toBe(
      'unknown:{"reason":"ServiceFailure"}',
    );
  });

  it("calls a stack that cannot read itself older than version tracking", () => {
    const d = verdict({ status: "outdated", deployed: null, reason: "unreadable" });
    expect(deployedLabel(t, d)).toBe("untagged");
    expect(roleTemplateSummary(t, d, false)).toBe('outdatedShort:{"deployed":"untagged","expected":1}');
  });
});

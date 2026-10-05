import { describe, expect, it } from "vitest";

import type { GovernanceGatewayDetail } from "./api";
import { toolPolicyPrefill } from "./governance";

const gateway = {
  actions: [
    { name: "crm___getCustomer", target_name: "crm" },
    { name: "crm___listOrders", target_name: "crm" },
    { name: "wiki___search", target_name: "wiki" },
  ],
} as unknown as GovernanceGatewayDetail;

describe("toolPolicyPrefill", () => {
  it("picks exactly the target's actions and a valid policy name", () => {
    expect(toolPolicyPrefill(gateway, "crm")).toEqual({
      name: "allow_crm",
      actions: ["crm___getCustomer", "crm___listOrders"],
    });
  });

  it("slugs target names AWS policy names refuse and caps the length", () => {
    const { name, actions } = toolPolicyPrefill(gateway, `my-target-${"x".repeat(60)}`);
    expect(name).toMatch(/^allow_my_target_x+$/);
    expect(name.length).toBe(48);
    expect(actions).toEqual([]);
  });
});

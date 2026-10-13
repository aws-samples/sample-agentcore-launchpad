import { describe, expect, it } from "vitest";

import type { Workspace } from "../../../lib/api";
import { workspaceAttention } from "./attention";

function ws(id: string, over: Partial<Workspace> = {}): Workspace {
  return {
    id,
    name: `ws-${id}`,
    account_id: "123456789012",
    region: "us-west-2",
    cross_account: false,
    bootstrap_status: "ready",
    is_default: false,
    created_at: null,
    updated_at: null,
    ...over,
  };
}

describe("workspace attention", () => {
  it("is empty for no workspaces and for ready / bootstrapping ones", () => {
    expect(workspaceAttention([])).toEqual([]);
    expect(workspaceAttention([ws("a"), ws("b", { bootstrap_status: "bootstrapping" })])).toEqual([]);
  });

  it("queues a failed bootstrap as act and a registered workspace as wait", () => {
    expect(workspaceAttention([ws("f", { bootstrap_status: "failed" })])).toMatchObject([
      { key: "f:failed", kind: "failed", signal: "act" },
    ]);
    expect(workspaceAttention([ws("r", { bootstrap_status: "registered" })])).toMatchObject([
      { key: "r:registered", kind: "registered", signal: "wait" },
    ]);
  });

  it("never queues the hub, whatever its status", () => {
    const hub = (bootstrap_status: Workspace["bootstrap_status"]) => ws("hub", { is_default: true, bootstrap_status });
    expect(workspaceAttention([hub("failed")])).toEqual([]);
    expect(workspaceAttention([hub("registered")])).toEqual([]);
  });

  it("puts act before wait and keeps the API order otherwise", () => {
    const entries = workspaceAttention([
      ws("r1", { bootstrap_status: "registered" }),
      ws("f1", { bootstrap_status: "failed" }),
      ws("ok"),
      ws("r2", { bootstrap_status: "registered" }),
      ws("f2", { bootstrap_status: "failed" }),
    ]);
    expect(entries.map((e) => e.workspace.id)).toEqual(["f1", "f2", "r1", "r2"]);
  });
});

import { describe, expect, it } from "vitest";

import type { AgentInfo } from "../../../lib/api";
import { attachedNamespace, attachTargets, contextPrefill, editAgentLink, newAgentLink } from "./attach";

const NS = "dc09046b-28af-4c7b-a03a-4fa63cda243b";
const HR = { id: NS, name: "AnyCompany HR" };

const agent = (over: Partial<AgentInfo> & { spec?: Record<string, unknown> }): AgentInfo =>
  ({
    id: over.name ?? "a",
    name: "a",
    method: "harness",
    status: "active",
    spec: {},
    system: null,
    ...over,
  }) as AgentInfo;

describe("the wizard prefill", () => {
  it("reads the namespace a link carries", () => {
    expect(contextPrefill(new URLSearchParams(`context_ns=${NS}&context_name=AnyCompany%20HR`))).toEqual({
      namespaceId: NS,
      namespaceName: "AnyCompany HR",
    });
    expect(contextPrefill(new URLSearchParams(`context_ns=${NS}`))).toEqual({ namespaceId: NS, namespaceName: "" });
  });

  it("ignores a missing or malformed id and bounds the name", () => {
    expect(contextPrefill(new URLSearchParams(""))).toBeNull();
    expect(contextPrefill(new URLSearchParams("context_ns=not-a-uuid&context_name=x"))).toBeNull();
    const long = contextPrefill(new URLSearchParams(`context_ns=${NS}&context_name=${"x".repeat(400)}`));
    expect(long?.namespaceName).toHaveLength(256);
  });

  it("builds links the wizard reads back", () => {
    const fresh = new URL(newAgentLink(HR, "harness"), "https://x");
    expect(fresh.pathname).toBe("/v2/agents");
    expect(fresh.searchParams.get("view")).toBe("new");
    expect(fresh.searchParams.get("method")).toBe("harness");
    expect(contextPrefill(fresh.searchParams)).toEqual({ namespaceId: NS, namespaceName: "AnyCompany HR" });
    const edit = new URL(editAgentLink("agent/1", HR), "https://x");
    expect(edit.searchParams.get("view")).toBe("edit");
    expect(edit.searchParams.get("id")).toBe("agent/1");
    expect(contextPrefill(edit.searchParams)?.namespaceId).toBe(NS);
    expect(newAgentLink({ id: NS, name: " " }, "zip_runtime")).not.toContain("context_name");
  });
});

describe("attach targets", () => {
  const rows = [
    agent({ name: "harness-ok" }),
    agent({ name: "strands-ok", method: "zip_runtime", spec: { protocol: "http" } }),
    agent({ name: "strands-a2a", method: "zip_runtime", spec: { protocol: "a2a" } }),
    agent({ name: "converted", method: "zip_runtime", spec: { code_bundle: { files: [] } } }),
    agent({ name: "container", method: "container" }),
    agent({ name: "deploying", status: "deploying" }),
    agent({ name: "preset", system: { key: "architect" } as AgentInfo["system"] }),
    agent({ name: "failed-harness", status: "failed" }),
  ];

  it("lists the Harness and HTTP Strands agents the member may redeploy", () => {
    expect(attachTargets(rows, true).map((a) => a.name)).toEqual(["harness-ok", "strands-ok", "failed-harness"]);
  });

  it("lists nothing without the deploy permission", () => {
    expect(attachTargets(rows, false)).toEqual([]);
  });

  it("reads the namespace an agent already attaches", () => {
    expect(attachedNamespace(agent({ spec: { context_layer: { namespace_id: NS, namespace_name: "HR" } } }))).toEqual({
      id: NS,
      name: "HR",
    });
    expect(attachedNamespace(agent({ spec: { context_layer: null } }))).toBeNull();
    expect(attachedNamespace(agent({}))).toBeNull();
  });
});

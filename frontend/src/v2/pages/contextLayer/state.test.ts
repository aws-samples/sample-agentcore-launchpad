import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  buildAgentSpec,
  contextLayerSpec,
  contextLayerValid,
  emptyAgentForm,
  formFromStoredSpec,
  republishSpec,
} from "../../../lib/agent-spec";
import type { ContextLayerNamespace, ContextLayerStatus } from "../../../lib/api";
import {
  authStatusOf,
  healthRows,
  namespaceOptions,
  pickerMode,
  pickNamespace,
  pollAuthorized,
  sectionVisible,
} from "./state";

const NS = "dc09046b-28af-4c7b-a03a-4fa63cda243b";
const connected: ContextLayerStatus = {
  connected: true,
  can_manage: false,
  authorization: { status: "authorized", authorized_at: null },
};
const hr: ContextLayerNamespace = {
  namespace_id: NS,
  name: "anycompany-hr",
  display_name: "AnyCompany HR",
  status: "ACTIVE",
  source_count: 2,
  created_at: null,
  updated_at: null,
};
const catalogs = {
  gatewayTargets: [],
  remoteMcp: [],
  storedGatewayConfig: {},
  kbInfo: (id: string) => ({ kb_id: id, name: id, description: "" }),
};

describe("section visibility", () => {
  it("shows for a connected workspace on harness and HTTP Strands only", () => {
    expect(sectionVisible(connected, { method: "harness", protocol: "http", contextLayer: null })).toBe(true);
    expect(sectionVisible(connected, { method: "zip_runtime", protocol: "http", contextLayer: null })).toBe(true);
    expect(sectionVisible(connected, { method: "zip_runtime", protocol: "a2a", contextLayer: null })).toBe(false);
    expect(sectionVisible(connected, { method: "container", protocol: "http", contextLayer: null })).toBe(false);
    expect(sectionVisible(connected, { method: "byoc", protocol: "http", contextLayer: null })).toBe(false);
  });

  it("hides when nothing is connected, unless the agent already attaches a namespace", () => {
    const off: ContextLayerStatus = { connected: false, can_manage: false };
    expect(sectionVisible(off, { method: "harness", protocol: "http", contextLayer: null })).toBe(false);
    expect(sectionVisible(null, { method: "harness", protocol: "http", contextLayer: null })).toBe(false);
    const attached = { namespaceId: NS, namespaceName: "" };
    expect(sectionVisible(off, { method: "harness", protocol: "http", contextLayer: attached })).toBe(true);
  });

  it("reads the caller's authorization, none while disconnected", () => {
    expect(authStatusOf(connected)).toBe("authorized");
    expect(authStatusOf({ connected: false, can_manage: false })).toBe("none");
    expect(authStatusOf(null)).toBe("none");
  });
});

describe("namespace picker", () => {
  it("picks from the list once authorized and readable, else falls back to a typed id", () => {
    expect(pickerMode("authorized", { data: [hr], error: null })).toBe("picker");
    expect(pickerMode("none", { data: [hr], error: null })).toBe("manual");
    expect(pickerMode("pending", { data: null, error: null })).toBe("manual");
    expect(pickerMode("authorized", { data: null, error: "COA refused" })).toBe("manual");
    expect(pickerMode("authorized", { data: null, error: null })).toBe("manual");
  });

  it("keeps a stored namespace COA no longer lists selectable", () => {
    expect(namespaceOptions([hr], null)).toEqual([{ value: NS, label: "AnyCompany HR" }]);
    const gone = { namespaceId: "00000000-0000-0000-0000-000000000000", namespaceName: "Old" };
    expect(namespaceOptions([hr], gone)).toEqual([
      { value: NS, label: "AnyCompany HR" },
      { value: gone.namespaceId, label: "Old" },
    ]);
  });

  it("denormalizes the picked namespace's name, clears on none", () => {
    expect(pickNamespace([hr], NS, null)).toEqual({ namespaceId: NS, namespaceName: "AnyCompany HR" });
    expect(pickNamespace([hr], "", null)).toBeNull();
    const kept = { namespaceId: "x", namespaceName: "Kept" };
    expect(pickNamespace([hr], "x", kept)).toEqual(kept);
  });
});

describe("deployment health", () => {
  it("maps live probes to signals; an unknown probe is info, never a failure", () => {
    const rows = healthRows({
      ...connected,
      can_manage: true,
      runtimes: { mcp: "READY", serve: "UPDATING" },
      agent_oauth_enabled: true,
      callback_registered: null,
    });
    expect(rows.map((r) => [r.key, r.signal, r.value])).toEqual([
      ["mcp", "ok", "READY"],
      ["serve", "act", "UPDATING"],
      ["agentOauth", "ok", true],
      ["callback", "info", null],
    ]);
  });

  it("shows the callback check to administrators only", () => {
    const keys = (can_manage: boolean) =>
      healthRows({ ...connected, can_manage, callback_registered: null }).map((r) => r.key);
    expect(keys(true)).toContain("callback");
    expect(keys(false)).not.toContain("callback");
  });
});

describe("pollAuthorized", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("stops once the consent completes, keeps waiting through a failed read", async () => {
    const answers: (string | Error)[] = ["pending", new Error("blip"), "authorized"];
    const onAuthorized = vi.fn();
    pollAuthorized({
      check: async () => {
        const next = answers.shift();
        if (next instanceof Error) throw next;
        return next as "pending" | "authorized";
      },
      onAuthorized,
      intervalMs: 10,
    });
    await vi.advanceTimersByTimeAsync(35);
    expect(onAuthorized).toHaveBeenCalledTimes(1);
    expect(answers).toEqual([]);
  });

  it("gives up after the limit, and a cancel stops it", async () => {
    const onExpired = vi.fn();
    let clock = 0;
    pollAuthorized({
      check: async () => "pending",
      onAuthorized: () => undefined,
      onExpired,
      intervalMs: 10,
      limitMs: 25,
      now: () => clock,
    });
    for (let i = 0; i < 5; i += 1) {
      clock += 10;
      await vi.advanceTimersByTimeAsync(10);
    }
    expect(onExpired).toHaveBeenCalledTimes(1);
    const check = vi.fn(async () => "pending" as const);
    const cancel = pollAuthorized({ check, onAuthorized: () => undefined, intervalMs: 10 });
    cancel();
    await vi.advanceTimersByTimeAsync(50);
    expect(check).not.toHaveBeenCalled();
  });
});

describe("the spec carries context_layer", () => {
  it("states it on V2 forms: the ref on harness / HTTP Strands, null otherwise", () => {
    const form = { ...emptyAgentForm("harness"), name: "hr-agent", systemPrompt: "help" };
    expect(buildAgentSpec(form, catalogs).context_layer).toBeNull();
    form.contextLayer = { namespaceId: NS, namespaceName: " AnyCompany HR " };
    expect(buildAgentSpec(form, catalogs).context_layer).toEqual({ namespace_id: NS, namespace_name: "AnyCompany HR" });
    expect(contextLayerSpec({ ...form, method: "container" })).toEqual({ context_layer: null });
    expect(contextLayerSpec({ ...form, method: "zip_runtime", protocol: "a2a" })).toEqual({ context_layer: null });
    expect(contextLayerSpec({ ...form, contextLayer: { namespaceId: NS, namespaceName: "" } })).toEqual({
      context_layer: { namespace_id: NS },
    });
  });

  it("omits it for a form that does not own it, so a re-publish carries the stored ref", () => {
    const form = { ...emptyAgentForm("harness"), name: "hr-agent", systemPrompt: "help" };
    delete form.contextLayer;
    const built = buildAgentSpec(form, catalogs);
    expect("context_layer" in built).toBe(false);
    const stored = { context_layer: { namespace_id: NS, namespace_name: "AnyCompany HR" } };
    expect(republishSpec(built, stored).context_layer).toEqual(stored.context_layer);
  });

  it("round-trips a stored ref through the form, and a V2 removal sticks", () => {
    const { form } = formFromStoredSpec("harness", "hr-agent", {
      system_prompt: "help",
      context_layer: { namespace_id: NS, namespace_name: "AnyCompany HR" },
    });
    expect(form.contextLayer).toEqual({ namespaceId: NS, namespaceName: "AnyCompany HR" });
    const removed = buildAgentSpec({ ...form, contextLayer: null }, catalogs);
    expect(republishSpec(removed, { context_layer: { namespace_id: NS } }).context_layer).toBeNull();
  });

  it("refuses a typed id that is not a UUID", () => {
    const form = { ...emptyAgentForm("harness"), contextLayer: { namespaceId: "not-a-uuid", namespaceName: "" } };
    expect(contextLayerValid(form)).toBe(false);
    expect(contextLayerValid({ ...form, contextLayer: { namespaceId: NS, namespaceName: "" } })).toBe(true);
    expect(contextLayerValid({ ...form, contextLayer: null })).toBe(true);
    // an unsupported method never sends it, so it never blocks
    expect(contextLayerValid({ ...form, method: "container" })).toBe(true);
  });
});

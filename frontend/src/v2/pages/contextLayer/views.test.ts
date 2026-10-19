import { describe, expect, it } from "vitest";

import type { ContextLayerStatus } from "../../../lib/api";
import {
  coaConsoleUrl,
  coaErrorKind,
  contextView,
  healthSignal,
  moduleGate,
  namespaceSignal,
  namespacesState,
  ontologySignal,
  sourceSignal,
  stepSignal,
  viewSearch,
} from "./views";

const NS = "dc09046b-28af-4c7b-a03a-4fa63cda243b";
const CONSOLE = "https://d3q37s5vts6pte.cloudfront.net";
const q = (s: string) => new URLSearchParams(s);

describe("view routing", () => {
  it("defaults to the overview", () => {
    expect(contextView(q(""))).toEqual({ view: "overview" });
    expect(contextView(q("view=whatever"))).toEqual({ view: "overview" });
  });

  it("opens a namespace on its sources tab, or the tab the URL names", () => {
    expect(contextView(q(`view=namespace&id=${NS}`))).toEqual({ view: "namespace", id: NS, tab: "sources" });
    expect(contextView(q(`view=namespace&id=${NS}&tab=ontology`))).toEqual({ view: "namespace", id: NS, tab: "ontology" });
    expect(contextView(q(`view=namespace&id=${NS}&tab=grants`)).view).toBe("namespace");
    expect((contextView(q(`view=namespace&id=${NS}&tab=grants`)) as { tab: string }).tab).toBe("sources");
  });

  it("never routes an id that is not a UUID", () => {
    expect(contextView(q("view=namespace&id=../grants"))).toEqual({ view: "overview" });
    expect(contextView(q("view=namespace"))).toEqual({ view: "overview" });
    expect(contextView(q("view=playground&ns=nope"))).toEqual({ view: "playground", ns: null });
  });

  it("opens the playground with or without a namespace", () => {
    expect(contextView(q(`view=playground&ns=${NS}`))).toEqual({ view: "playground", ns: NS });
    expect(contextView(q("view=playground"))).toEqual({ view: "playground", ns: null });
  });

  it("round-trips every view through its search params, defaults omitted", () => {
    for (const view of [
      { view: "overview" as const },
      { view: "namespace" as const, id: NS, tab: "sources" as const },
      { view: "namespace" as const, id: NS, tab: "metrics" as const },
      { view: "playground" as const, ns: NS },
      { view: "playground" as const, ns: null },
    ]) {
      expect(contextView(new URLSearchParams(viewSearch(view)))).toEqual(view);
    }
    expect(viewSearch({ view: "namespace", id: NS, tab: "sources" })).toEqual({ view: "namespace", id: NS });
  });
});

describe("COA console deep links", () => {
  it("lands on the matching COA console routes", () => {
    expect(coaConsoleUrl(CONSOLE, "sources", NS)).toBe(`${CONSOLE}/namespaces/${NS}/sources`);
    expect(coaConsoleUrl(CONSOLE, "metrics", NS)).toBe(`${CONSOLE}/namespaces/${NS}/metrics`);
    expect(coaConsoleUrl(CONSOLE, "ontology", NS)).toBe(`${CONSOLE}/namespaces/${NS}/ontology/graph`);
    expect(coaConsoleUrl(CONSOLE, "namespace", NS)).toBe(`${CONSOLE}/namespaces/${NS}`);
    expect(coaConsoleUrl(CONSOLE, "playground")).toBe(`${CONSOLE}/playground`);
    expect(coaConsoleUrl(CONSOLE, "home")).toBe(`${CONSOLE}/`);
  });

  it("tolerates a trailing slash or a base path", () => {
    expect(coaConsoleUrl(`${CONSOLE}/`, "metrics", NS)).toBe(`${CONSOLE}/namespaces/${NS}/metrics`);
    expect(coaConsoleUrl(`${CONSOLE}/coa/`, "playground")).toBe(`${CONSOLE}/coa/playground`);
  });

  it("refuses anything but an https console and encodes the id", () => {
    expect(coaConsoleUrl("http://example.com", "sources", NS)).toBeNull();
    expect(coaConsoleUrl("javascript:alert(1)", "sources", NS)).toBeNull();
    expect(coaConsoleUrl("", "sources", NS)).toBeNull();
    expect(coaConsoleUrl(null, "playground")).toBeNull();
    expect(coaConsoleUrl(CONSOLE, "sources")).toBeNull();
    expect(coaConsoleUrl(CONSOLE, "sources", "a/b")).toBe(`${CONSOLE}/namespaces/a%2Fb/sources`);
  });
});

describe("empty states", () => {
  const status = (s: Partial<ContextLayerStatus>): ContextLayerStatus => ({ connected: true, can_manage: false, ...s });

  it("gates a sub-page on connection, then on the member's authorization", () => {
    expect(moduleGate({ data: null, loading: true, error: null })).toBe("loading");
    expect(moduleGate({ data: null, loading: false, error: "boom" })).toBe("error");
    expect(moduleGate({ data: { connected: false, can_manage: true }, loading: false, error: null })).toBe("notConnected");
    for (const s of ["none", "pending", "revoked"] as const) {
      expect(moduleGate({ data: status({ authorization: { status: s, authorized_at: null } }), loading: false, error: null })).toBe("needAuth");
    }
    expect(moduleGate({ data: status({}), loading: false, error: null })).toBe("needAuth");
    expect(
      moduleGate({ data: status({ authorization: { status: "authorized", authorized_at: null } }), loading: false, error: null }),
    ).toBe("ready");
  });

  it("asks for consent before listing namespaces, then explains an empty list", () => {
    expect(namespacesState("none", { data: null, error: null, loading: false })).toBe("needAuth");
    expect(namespacesState("pending", { data: [1], error: null, loading: false })).toBe("needAuth");
    expect(namespacesState("authorized", { data: null, error: null, loading: true })).toBe("loading");
    expect(namespacesState("authorized", { data: null, error: "x", loading: false })).toBe("error");
    expect(namespacesState("authorized", { data: [], error: null, loading: false })).toBe("empty");
    expect(namespacesState("authorized", { data: [1], error: null, loading: false })).toBe("list");
  });

  it("names how each COA error is presented", () => {
    expect(coaErrorKind("context_layer.authorization_required")).toBe("authorize");
    expect(coaErrorKind("context_layer.reauthorize")).toBe("reauthorize");
    expect(coaErrorKind("context_layer.forbidden")).toBe("forbidden");
    expect(coaErrorKind("context_layer.coa_not_found")).toBe("notFound");
    expect(coaErrorKind("context_layer.not_connected")).toBe("notConnected");
    expect(coaErrorKind("context_layer.upstream")).toBe("other");
    expect(coaErrorKind(undefined)).toBe("other");
  });
});

describe("status signals", () => {
  it("maps COA source statuses, keeping unknown ones neutral", () => {
    expect(sourceSignal("COMPLETED")).toBe("ok");
    expect(sourceSignal("APPROVED")).toBe("ok");
    expect(sourceSignal("PENDING_REVIEW")).toBe("wait");
    expect(sourceSignal("APPROVING")).toBe("wait");
    expect(sourceSignal("SCANNING_KG_BUILD")).toBe("info");
    expect(sourceSignal("ENRICHING")).toBe("info");
    expect(sourceSignal("SCAN_FAILED")).toBe("act");
    expect(sourceSignal("SOMETHING_NEW")).toBe("off");
    expect(sourceSignal(null)).toBe("off");
  });

  it("maps namespace, health, ontology and step statuses", () => {
    expect(namespaceSignal("ACTIVE")).toBe("ok");
    expect(namespaceSignal("CREATING")).toBe("info");
    expect(namespaceSignal("DELETING")).toBe("off");
    expect(healthSignal("ok")).toBe("ok");
    expect(healthSignal("degraded")).toBe("wait");
    expect(healthSignal("error: collection unreachable")).toBe("act");
    expect(healthSignal(null)).toBe("info");
    expect(ontologySignal("ok")).toBe("ok");
    expect(ontologySignal("parse_error")).toBe("act");
    expect(ontologySignal("deleting")).toBe("off");
    expect(stepSignal("success")).toBe("ok");
    expect(stepSignal("skipped")).toBe("off");
    expect(stepSignal("failed")).toBe("act");
    expect(stepSignal("running")).toBe("info");
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";
import { stampSelection, storedWorkspaceId } from "./workspace-header";

afterEach(() => vi.unstubAllGlobals());

function response() {
  const fetch = vi.fn<typeof globalThis.fetch>(async () => new Response(JSON.stringify({ items: [], next_token: null }), { status: 200 }));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

function params(path: string) { return new URL(path, "http://localhost").searchParams; }

describe("Memory client contracts", () => {
  it("keeps unfiltered requests backward-compatible", async () => {
    const fetch = response();
    await api.memoryNamespaces("a__human");
    expect(params(fetch.mock.calls[0][0] as string).has("session_id")).toBe(false);
    await api.memoryRecords({ actor_id: "a__human", strategy_id: "summary" });
    expect(params(fetch.mock.calls[1][0] as string).has("record_type")).toBe(false);
    await api.memorySearchRecords({ query: "q", actor_id: "a__human" });
    expect(JSON.parse((fetch.mock.calls[2][1] as RequestInit).body as string)).toEqual({ query: "q", actor_id: "a__human" });
  });

  it("encodes exact session scope, source and pagination", async () => {
    const fetch = response();
    await api.memoryNamespaces("a__human", "session#1");
    expect(params(fetch.mock.calls[0][0] as string).get("session_id")).toBe("session#1");
    await api.memoryRecords({ actor_id: "a__human", strategy_id: "summary", session_id: "session#1",
      namespace: "/summaries/a__human/session#1/", source: "shared", record_type: "FULL_SESSION" }, "next+token");
    const query = params(fetch.mock.calls[1][0] as string);
    expect(Object.fromEntries(query)).toEqual({ actor_id: "a__human", strategy_id: "summary", session_id: "session#1",
      namespace: "/summaries/a__human/session#1/", source: "shared", record_type: "FULL_SESSION", next_token: "next+token" });
  });

  it("pins discovery, records, pagination and retrieval when another tab changes storage", async () => {
    const storage = new Map([["launchpad_workspace", "original"], ["launchpad_project:original", "project-one"]]);
    vi.stubGlobal("window", { localStorage: { getItem: (key: string) => storage.get(key) ?? null } });
    const targets: { workspaceId: string | null; projectId: string | null }[] = [];
    let finishDiscovery!: () => void;
    vi.stubGlobal("fetch", vi.fn<typeof globalThis.fetch>((input, init) => {
      targets.push(stampSelection(new Headers(init?.headers), storedWorkspaceId()));
      const reply = () => new Response(JSON.stringify({ items: [], next_token: null }), { status: 200 });
      if (String(input).includes("/namespaces")) return new Promise<Response>((resolve) => {
        finishDiscovery = () => resolve(reply());
      });
      return Promise.resolve(reply());
    }));
    const scope = { workspaceId: "original", projectId: "project-one" };
    const discovery = api.memoryNamespaces("a__human", "session-1", scope);
    storage.set("launchpad_workspace", "another");
    storage.set("launchpad_project:original", "project-two");
    storage.set("launchpad_project:another", "project-three");
    finishDiscovery();
    await discovery;
    await api.memoryRecords({ actor_id: "a__human", session_id: "session-1", record_type: "FULL_SESSION" }, null, scope);
    await api.memoryRecords({ actor_id: "a__human", session_id: "session-1", record_type: "FULL_SESSION" }, "next", scope);
    await api.memorySearchRecords({ actor_id: "a__human", query: "q", record_type: "FULL_SESSION" }, scope);
    expect(targets).toEqual(Array.from({ length: 4 }, () => ({ workspaceId: "original", projectId: "project-one" })));
  });

  it("includes the full-session filter in the retrieval body", async () => {
    const fetch = response();
    const input = { query: "decision", actor_id: "a__human", strategy_id: "summary", record_type: "FULL_SESSION" as const, top_k: 3 };
    await api.memorySearchRecords(input);
    expect(fetch.mock.calls[0][0]).toBe("/api/memory/records/search");
    expect((fetch.mock.calls[0][1] as RequestInit).method).toBe("POST");
    expect(JSON.parse((fetch.mock.calls[0][1] as RequestInit).body as string)).toEqual(input);
  });
});

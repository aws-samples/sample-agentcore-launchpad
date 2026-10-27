import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  clearProjectId,
  PROJECT_HEADER,
  PROJECT_INVALID_EVENT,
  PROJECT_STORAGE_PREFIX,
  projectResetCode,
  stampSelection,
  storedProjectId,
  storeProjectId,
  WORKSPACE_HEADER,
  WORKSPACE_STORAGE_KEY,
} from "./workspace-header";

/** A `window` with just what the module touches: storage, location, fetch, events. */
function stubWindow(fetchImpl: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>) {
  const store = new Map<string, string>();
  const events: Event[] = [];
  const network = vi.fn(fetchImpl);
  const win = {
    localStorage: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    },
    location: { href: "http://console.test/v3", origin: "http://console.test" },
    fetch: network as typeof fetch,
    dispatchEvent: (e: Event) => {
      events.push(e);
      return true;
    },
  };
  vi.stubGlobal("window", win);
  // `network` stays the underlying fetch once the wrapper replaces `win.fetch`
  return { store, events, win, network };
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("project storage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("keeps one project per workspace and refuses malformed ids", () => {
    const { store } = stubWindow(async () => json(200, {}));
    storeProjectId("dev", "risk-bot");
    storeProjectId("prod", "default");
    expect(storedProjectId("dev")).toBe("risk-bot");
    expect(storedProjectId("prod")).toBe("default");
    expect(store.get(`${PROJECT_STORAGE_PREFIX}dev`)).toBe("risk-bot");
    store.set(`${PROJECT_STORAGE_PREFIX}dev`, "Bad Id!");
    expect(storedProjectId("dev")).toBeNull();
    expect(storedProjectId(null)).toBeNull();
    clearProjectId("prod");
    expect(storedProjectId("prod")).toBeNull();
  });
});

describe("stampSelection", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("stamps the stored workspace and its project", () => {
    stubWindow(async () => json(200, {}));
    storeProjectId("dev", "risk-bot");
    const headers = new Headers();
    expect(stampSelection(headers, "dev")).toEqual({ workspaceId: "dev", projectId: "risk-bot" });
    expect(headers.get(WORKSPACE_HEADER)).toBe("dev");
    expect(headers.get(PROJECT_HEADER)).toBe("risk-bot");
  });

  it("sends no project when none is remembered (the backend default applies)", () => {
    stubWindow(async () => json(200, {}));
    const headers = new Headers();
    expect(stampSelection(headers, "dev")).toEqual({ workspaceId: "dev", projectId: null });
    expect(headers.has(PROJECT_HEADER)).toBe(false);
  });

  it("a pinned workspace carries its own project, never the current one's", () => {
    stubWindow(async () => json(200, {}));
    storeProjectId("dev", "risk-bot");
    storeProjectId("prod", "collections");
    const pinned = new Headers({ [WORKSPACE_HEADER]: "prod" });
    expect(stampSelection(pinned, "dev")).toEqual({ workspaceId: "prod", projectId: "collections" });
    const unknown = new Headers({ [WORKSPACE_HEADER]: "staging" });
    stampSelection(unknown, "dev");
    expect(unknown.has(PROJECT_HEADER)).toBe(false);
  });

  it("keeps a project the caller set", () => {
    stubWindow(async () => json(200, {}));
    storeProjectId("dev", "risk-bot");
    const headers = new Headers({ [PROJECT_HEADER]: "other" });
    expect(stampSelection(headers, "dev").projectId).toBe("other");
  });

  it("stamps nothing without any workspace", () => {
    stubWindow(async () => json(200, {}));
    const headers = new Headers();
    expect(stampSelection(headers, null)).toEqual({ workspaceId: null, projectId: null });
    expect([...headers.keys()]).toEqual([]);
  });
});

describe("projectResetCode", () => {
  it("recognises the three stale-selection refusals", () => {
    expect(projectResetCode(400, { code: "project.header_required" }, null)).toBe("project.header_required");
    expect(projectResetCode(403, { code: "project.none" }, "p1")).toBe("project.none");
    expect(
      projectResetCode(404, { code: "project.not_found", detail: { project_id: "p1" } }, "p1"),
    ).toBe("project.not_found");
  });

  it("ignores a not-found about another project (a path id) or with no project sent", () => {
    expect(projectResetCode(404, { code: "project.not_found", detail: { project_id: "p2" } }, "p1")).toBeNull();
    expect(projectResetCode(404, { code: "project.not_found", detail: { project_id: "p1" } }, null)).toBeNull();
  });

  it("ignores other codes, statuses and bodies", () => {
    expect(projectResetCode(403, { code: "auth.forbidden" }, "p1")).toBeNull();
    expect(projectResetCode(409, { code: "project.archived" }, "p1")).toBeNull();
    expect(projectResetCode(400, { code: "project.none" }, "p1")).toBeNull();
    expect(projectResetCode(404, null, "p1")).toBeNull();
    expect(projectResetCode(200, { code: "project.none" }, "p1")).toBeNull();
  });
});

describe("installWorkspaceHeader", () => {
  let fetchImpl: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
  let env: ReturnType<typeof stubWindow>;
  let install: () => void;

  beforeEach(async () => {
    fetchImpl = async () => json(200, {});
    env = stubWindow((input, init) => fetchImpl(input, init));
    vi.stubGlobal("CustomEvent", class extends Event {
      detail: unknown;
      constructor(type: string, init?: { detail?: unknown }) {
        super(type);
        this.detail = init?.detail;
      }
    });
    vi.resetModules(); // the wrapper installs once per module instance
    ({ installWorkspaceHeader: install } = await import("./workspace-header"));
    install();
  });
  afterEach(() => vi.unstubAllGlobals());

  const sentHeaders = () => new Headers(env.network.mock.calls.at(-1)?.[1]?.headers);

  it("stamps both headers on /api calls and leaves other origins alone", async () => {
    env.store.set(WORKSPACE_STORAGE_KEY, "dev");
    env.store.set(`${PROJECT_STORAGE_PREFIX}dev`, "risk-bot");
    await window.fetch("/api/agents");
    expect(sentHeaders().get(WORKSPACE_HEADER)).toBe("dev");
    expect(sentHeaders().get(PROJECT_HEADER)).toBe("risk-bot");
    await window.fetch("https://elsewhere.test/api/x");
    expect(sentHeaders().has(PROJECT_HEADER)).toBe(false);
  });

  it("clears the stored project and announces a refusal of it", async () => {
    env.store.set(WORKSPACE_STORAGE_KEY, "dev");
    env.store.set(`${PROJECT_STORAGE_PREFIX}dev`, "gone");
    fetchImpl = async () => json(404, { code: "project.not_found", detail: { project_id: "gone" } });
    const res = await window.fetch("/api/agents");
    // the caller still reads its own body
    expect(await res.json()).toMatchObject({ code: "project.not_found" });
    await vi.waitFor(() => expect(env.events).toHaveLength(1));
    const event = env.events[0] as Event & { detail: unknown };
    expect(event.type).toBe(PROJECT_INVALID_EVENT);
    expect(event.detail).toEqual({ workspaceId: "dev", projectId: "gone", code: "project.not_found" });
    expect(env.store.has(`${PROJECT_STORAGE_PREFIX}dev`)).toBe(false);
  });

  it("a create naming its project sends it in body and header over a `default` selection (#351)", async () => {
    env.store.set(WORKSPACE_STORAGE_KEY, "dev");
    env.store.set(`${PROJECT_STORAGE_PREFIX}dev`, "default");
    fetchImpl = async () => json(202, { agent: {}, job_id: "j", deployment_id: "d" });
    // the typed client calls the bare global fetch — route it through the wrapper
    vi.stubGlobal("fetch", window.fetch);
    const { api } = await import("./api");
    await api.createAgent({ name: "helper", method: "harness", system_prompt: "Be brief.", project_id: "risk-bot" });
    const init = env.network.mock.calls.at(-1)?.[1];
    expect(JSON.parse(String(init?.body))).toMatchObject({ project_id: "risk-bot" });
    expect(sentHeaders().get(PROJECT_HEADER)).toBe("risk-bot");
    expect(sentHeaders().get(WORKSPACE_HEADER)).toBe("dev");
  });

  it("a coding deploy naming its project sends it in body and header (#356)", async () => {
    env.store.set(WORKSPACE_STORAGE_KEY, "dev");
    env.store.set(`${PROJECT_STORAGE_PREFIX}dev`, "default");
    fetchImpl = async () => json(202, { agent_id: "a", job_id: "j", created: true });
    vi.stubGlobal("fetch", window.fetch);
    // a fresh module graph re-initialises i18n, which sets <html lang>
    vi.stubGlobal("document", { documentElement: {} });
    const { codingApi } = await import("./api");
    await codingApi.deploy("repo1", { agent_name: "helper", project_id: "risk-bot" });
    const init = env.network.mock.calls.at(-1)?.[1];
    expect(JSON.parse(String(init?.body))).toMatchObject({ project_id: "risk-bot" });
    expect(sentHeaders().get(PROJECT_HEADER)).toBe("risk-bot");
  });

  it("stays quiet on an unrelated error", async () => {
    env.store.set(WORKSPACE_STORAGE_KEY, "dev");
    env.store.set(`${PROJECT_STORAGE_PREFIX}dev`, "risk-bot");
    fetchImpl = async () => json(403, { code: "auth.forbidden" });
    await window.fetch("/api/agents");
    await new Promise((r) => setTimeout(r, 0));
    expect(env.events).toHaveLength(0);
    expect(env.store.get(`${PROJECT_STORAGE_PREFIX}dev`)).toBe("risk-bot");
  });
});

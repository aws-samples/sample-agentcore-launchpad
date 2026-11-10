// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, withSignal } from "./api";
import { WORKSPACE_HEADER, WORKSPACE_STORAGE_KEY } from "./workspace-header";

const response = (body: unknown) =>
  new Response(JSON.stringify(body), {headers: {"Content-Type": "application/json"}});
const primary = {
  account_id: "123456789012", region: "cn-northwest-1",
  gateways: [{id: "gw_home", arn: "arn:aws-cn:bedrock-agentcore:cn-northwest-1:123456789012:gateway/gw_home"}],
};
const secondary = {
  account_id: primary.account_id, region: "cn-north-1",
  gateways: [{id: "gw_other", arn: "arn:aws-cn:bedrock-agentcore:cn-north-1:123456789012:gateway/gw_other"}],
};
const workspace = (id: string, region: string, account_id = primary.account_id, bootstrap_status = "ready") =>
  ({id, region, account_id, bootstrap_status});
let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  localStorage.setItem(WORKSPACE_STORAGE_KEY, "home");
});
afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("policy attachment gateway candidates", () => {
  it("keeps the ordinary governance list scoped to its single region", async () => {
    fetchMock.mockResolvedValue(response(primary));
    expect(await api.listGovernanceGateways()).toEqual(primary);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("reads only visible ready regions in the same account, with explicit workspace headers", async () => {
    fetchMock.mockImplementation(async (path: string, init: RequestInit) => {
      if (path === "/api/workspaces") return response({workspaces: [
        workspace("home", primary.region),
        workspace("beijing", secondary.region),
        workspace("unready", "cn-other-1", primary.account_id, "registered"),
        workspace("foreign", "cn-north-1", "999999999999"),
      ]});
      const ws = new Headers(init.headers).get(WORKSPACE_HEADER);
      if (ws === "home") return response(primary);
      if (ws === "beijing") return response(secondary);
      throw new Error(`unexpected workspace ${ws}`);
    });
    const result = await api.listGovernanceGateways(false, {
      workspaceId: "home", includeWorkspaceRegions: true,
    });
    expect(result.gateways.map(g => g.id)).toEqual(["gw_home", "gw_other"]);
    expect(result.region).toBe(primary.region);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(localStorage.getItem(WORKSPACE_STORAGE_KEY)).toBe("home");
  });

  it("carries page cancellation into the later regional request and propagates its error", async () => {
    const controller = new AbortController();
    fetchMock.mockImplementation(async (path: string, init: RequestInit) => {
      expect(init.signal).toBe(controller.signal);
      if (path === "/api/workspaces") return response({
        workspaces: [workspace("beijing", secondary.region)],
      });
      if (new Headers(init.headers).get(WORKSPACE_HEADER) === "home") return response(primary);
      return new Response(JSON.stringify({code: "workspace.forbidden", message: "no longer granted"}),
        {status: 403, headers: {"Content-Type": "application/json"}});
    });
    await expect(withSignal(controller.signal, () => api.listGovernanceGateways(false, {
      workspaceId: "home", includeWorkspaceRegions: true,
    }))).rejects.toMatchObject({code: "workspace.forbidden"});
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});

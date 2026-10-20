import { describe, expect, it } from "vitest";

import type { ContextLayerGatewayStatus } from "../../../lib/api";
import { gatewayActions, gatewaySignal } from "./state";

const ready: Pick<ContextLayerGatewayStatus, "enabled" | "state" | "issues"> = {
  enabled: true,
  state: "ready",
  issues: [],
};

describe("gateway signal", () => {
  it("maps the lifecycle onto the shared vocabulary", () => {
    expect(gatewaySignal(ready)).toBe("ok");
    expect(gatewaySignal({ state: "creating", issues: [] })).toBe("info");
    expect(gatewaySignal({ state: "failed", issues: [] })).toBe("act");
    expect(gatewaySignal({ state: "missing", issues: [] })).toBe("act");
    expect(gatewaySignal({ state: "unknown", issues: [] })).toBe("wait");
    expect(gatewaySignal({ state: "disabled", issues: [] })).toBe("off");
  });

  it("flags a ready gateway that drifted", () => {
    expect(gatewaySignal({ state: "ready", issues: ["return_url"] })).toBe("act");
  });
});

describe("gateway actions", () => {
  it("offers members nothing", () => {
    expect(gatewayActions(ready, false)).toEqual({ enable: false, repair: false, refresh: false, remove: false });
    expect(gatewayActions({ enabled: false, state: "disabled", issues: [] }, false).enable).toBe(false);
  });

  it("offers nothing before the status loaded", () => {
    expect(gatewayActions(null, true)).toEqual({ enable: false, repair: false, refresh: false, remove: false });
  });

  it("offers enable while disabled", () => {
    expect(gatewayActions({ enabled: false, state: "disabled", issues: [] }, true)).toEqual({
      enable: true,
      repair: false,
      refresh: false,
      remove: false,
    });
  });

  it("offers refresh and delete on a healthy gateway", () => {
    expect(gatewayActions(ready, true)).toEqual({ enable: false, repair: false, refresh: true, remove: true });
  });

  it("offers a repair on a broken or drifted one, not while it settles", () => {
    expect(gatewayActions({ enabled: true, state: "failed", issues: [] }, true)).toMatchObject({ repair: true, refresh: false, remove: true });
    expect(gatewayActions({ enabled: true, state: "missing", issues: [] }, true).repair).toBe(true);
    expect(gatewayActions({ enabled: true, state: "ready", issues: ["endpoint"] }, true)).toMatchObject({ repair: true, refresh: true });
    expect(gatewayActions({ enabled: true, state: "creating", issues: [] }, true)).toMatchObject({ repair: false, remove: true });
  });
});

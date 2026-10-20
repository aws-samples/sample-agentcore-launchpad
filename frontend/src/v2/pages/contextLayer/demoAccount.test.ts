import { afterEach, describe, expect, it, vi } from "vitest";

import type { ContextLayerDemoInfo } from "../../../lib/api";
import { DEMO_INFO_TTL_MS, loadDemoInfo, resetDemoInfo } from "./demoInfo";
import {
  ACCOUNT_INTENT_KEY,
  accountView,
  choiceSettled,
  demoOffered,
  otherAccount,
  readAccountIntent,
  writeAccountIntent,
} from "./state";

const info: ContextLayerDemoInfo = { available: true, username: "launchpad-demo@coa-demo.example.com", connection: "coa-w2" };

function memoryStore(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (k: string) => data.get(k) ?? null,
    setItem: (k: string, v: string) => void data.set(k, v),
    removeItem: (k: string) => void data.delete(k),
    data,
  };
}

describe("accountView", () => {
  it("offers the choice while not authorized", () => {
    for (const auth of ["none", "pending", "revoked"] as const) {
      expect(accountView({ auth, identity: null, intent: null, guide: null })).toEqual({ kind: "choose" });
      // an identity left over from an earlier read never counts while unauthorized
      expect(accountView({ auth, identity: "demo", intent: "demo", guide: null })).toEqual({ kind: "choose" });
    }
  });

  it("tags an authorization as the demo account; own and unknown read as own", () => {
    expect(accountView({ auth: "authorized", identity: "demo", intent: null, guide: null })).toEqual({ kind: "demo" });
    expect(accountView({ auth: "authorized", identity: "own", intent: null, guide: null })).toEqual({ kind: "own" });
    expect(accountView({ auth: "authorized", identity: null, intent: "demo", guide: null })).toEqual({ kind: "own" });
    expect(accountView({ auth: "authorized", identity: undefined, intent: null, guide: null })).toEqual({ kind: "own" });
  });

  it("opens the guide when a demo consent resolved to the member's own account", () => {
    expect(accountView({ auth: "authorized", identity: "own", intent: "demo", guide: null })).toEqual({
      kind: "guide",
      target: "demo",
      revoked: false,
      wrong: true,
    });
    // and the other way round: a cached demo session answered an own consent
    expect(accountView({ auth: "authorized", identity: "demo", intent: "own", guide: null })).toMatchObject({
      kind: "guide",
      target: "own",
      wrong: true,
    });
  });

  it("keeps the guide through the revoke, and closes it once the target is authorized", () => {
    const opened = accountView({ auth: "authorized", identity: "demo", intent: null, guide: "own" });
    expect(opened).toEqual({ kind: "guide", target: "own", revoked: false, wrong: false });
    expect(accountView({ auth: "revoked", identity: null, intent: null, guide: "own" })).toEqual({
      kind: "guide",
      target: "own",
      revoked: true,
      wrong: false,
    });
    expect(accountView({ auth: "pending", identity: null, intent: "own", guide: "own" })).toMatchObject({ kind: "guide", revoked: true });
    // still the demo session (the member skipped the sign-out): the guide stays, warning on
    expect(accountView({ auth: "authorized", identity: "demo", intent: "own", guide: "own" })).toEqual({
      kind: "guide",
      target: "own",
      revoked: false,
      wrong: true,
    });
    expect(accountView({ auth: "authorized", identity: "own", intent: "own", guide: "own" })).toEqual({ kind: "own" });
    // an unknown identity has nothing to compare with: the guide does not trap the member
    expect(accountView({ auth: "authorized", identity: null, intent: "own", guide: "own" })).toEqual({ kind: "own" });
  });
});

describe("choiceSettled", () => {
  it("settles a choice once authorized as anything but the other account", () => {
    expect(choiceSettled(null, "authorized", "demo")).toBe(false);
    expect(choiceSettled("demo", "pending", null)).toBe(false);
    expect(choiceSettled("demo", "authorized", "own")).toBe(false);
    expect(choiceSettled("demo", "authorized", "demo")).toBe(true);
    expect(choiceSettled("own", "authorized", null)).toBe(true);
    expect(otherAccount("demo")).toBe("own");
    expect(otherAccount("own")).toBe("demo");
  });
});

describe("account intent", () => {
  it("round-trips through the tab's storage and ignores anything else", () => {
    const store = memoryStore();
    expect(readAccountIntent(store)).toBeNull();
    writeAccountIntent("demo", store);
    expect(store.data.get(ACCOUNT_INTENT_KEY)).toBe("demo");
    expect(readAccountIntent(store)).toBe("demo");
    writeAccountIntent(null, store);
    expect(store.data.has(ACCOUNT_INTENT_KEY)).toBe(false);
    expect(readAccountIntent(memoryStore({ [ACCOUNT_INTENT_KEY]: "admin" }))).toBeNull();
  });

  it("survives storage that is missing or throws", () => {
    const broken = {
      getItem: () => {
        throw new Error("denied");
      },
      setItem: () => {
        throw new Error("denied");
      },
      removeItem: () => {
        throw new Error("denied");
      },
    };
    expect(readAccountIntent(broken)).toBeNull();
    expect(() => writeAccountIntent("demo", broken)).not.toThrow();
    expect(readAccountIntent(null)).toBeNull();
    expect(() => writeAccountIntent("own", null)).not.toThrow();
  });
});

describe("demoOffered", () => {
  it("offers the demo account only on an ask for the layer's own Connection", () => {
    expect(demoOffered({ provider: "coa-w2" }, info)).toBe(true);
    expect(demoOffered({ provider: "github" }, info)).toBe(false);
    expect(demoOffered({ provider: "coa-w2" }, { ...info, available: false })).toBe(false);
    expect(demoOffered({ provider: "" }, { ...info, connection: null })).toBe(false);
    expect(demoOffered({ provider: "coa-w2" }, null)).toBe(false);
  });
});

describe("loadDemoInfo", () => {
  afterEach(() => resetDemoInfo());

  it("reads once per workspace until the TTL passes", async () => {
    let now = 1_000;
    const fetcher = vi.fn(async () => info);
    await loadDemoInfo("default", fetcher, () => now);
    await loadDemoInfo("default", fetcher, () => now);
    expect(fetcher).toHaveBeenCalledTimes(1);
    await loadDemoInfo("ws-2", fetcher, () => now);
    expect(fetcher).toHaveBeenCalledTimes(2);
    now += DEMO_INFO_TTL_MS;
    await loadDemoInfo("default", fetcher, () => now);
    expect(fetcher).toHaveBeenCalledTimes(3);
  });

  it("does not keep a failed read", async () => {
    const failing = vi.fn(async (): Promise<ContextLayerDemoInfo> => {
      throw new Error("offline");
    });
    await expect(loadDemoInfo("default", failing)).rejects.toThrow("offline");
    const fetcher = vi.fn(async () => info);
    await expect(loadDemoInfo("default", fetcher)).resolves.toEqual(info);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});

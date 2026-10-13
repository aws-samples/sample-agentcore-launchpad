import { afterEach, describe, expect, it, vi } from "vitest";

import { defaultNavMode, NAV_MODE_KEY, parseNavMode } from "./nav-mode";

/** A Map-backed localStorage stand-in (vitest runs in node, without one). */
function memoryStorage(initial: Record<string, string> = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => void data.set(key, value),
    removeItem: (key: string) => void data.delete(key),
    data,
  };
}

const throwingStorage = {
  getItem: () => {
    throw new Error("blocked");
  },
  setItem: () => {
    throw new Error("blocked");
  },
};

/** A fresh copy of the store module, which reads storage once at import. */
async function loadStore(storage: unknown) {
  vi.resetModules();
  vi.stubGlobal("localStorage", storage);
  return import("./nav-mode");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("parseNavMode", () => {
  it("accepts the two modes", () => {
    expect(parseNavMode("business")).toBe("business");
    expect(parseNavMode("expert")).toBe("expert");
  });

  it("rejects anything else", () => {
    expect(parseNavMode(null)).toBeNull();
    expect(parseNavMode("")).toBeNull();
    expect(parseNavMode("Business")).toBeNull();
    expect(parseNavMode('"expert"')).toBeNull();
  });
});

describe("nav mode store", () => {
  it("defaults to expert", async () => {
    expect(defaultNavMode()).toBe("expert");
    const store = await loadStore(memoryStorage());
    expect(store.getNavMode()).toBe("expert");
  });

  it("uses the stored value", async () => {
    const store = await loadStore(memoryStorage({ [NAV_MODE_KEY]: "business" }));
    expect(store.getNavMode()).toBe("business");
  });

  it("falls back to the default on an invalid stored value", async () => {
    const store = await loadStore(memoryStorage({ [NAV_MODE_KEY]: "slim" }));
    expect(store.getNavMode()).toBe("expert");
  });

  it("remembers a choice", async () => {
    const storage = memoryStorage();
    const store = await loadStore(storage);
    store.setNavMode("business");
    expect(store.getNavMode()).toBe("business");
    expect(storage.data.get(NAV_MODE_KEY)).toBe("business");
  });

  it("works in memory when storage throws", async () => {
    const store = await loadStore(throwingStorage);
    expect(store.getNavMode()).toBe("expert");
    expect(() => store.setNavMode("business")).not.toThrow();
    expect(store.getNavMode()).toBe("business");
  });

  it("falls back to the default when there is no storage at all", async () => {
    const store = await loadStore(undefined);
    expect(store.getNavMode()).toBe("expert");
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";

/** A fresh module over `stored`: the choice is read once, at import. */
async function load(stored: string | null) {
  vi.stubGlobal("localStorage", { getItem: () => stored, setItem: () => undefined });
  vi.resetModules();
  return import("./ui-version");
}

afterEach(() => vi.unstubAllGlobals());

describe("ui-version", () => {
  it("defaults to V2 when nothing is stored", async () => {
    expect((await load(null)).getUiVersion()).toBe("v2");
  });

  it("keeps an explicit V3 choice", async () => {
    expect((await load("v3")).getUiVersion()).toBe("v3");
  });

  it("reads an earlier classic choice as V2: the classic console is hidden", async () => {
    expect((await load("v1")).getUiVersion()).toBe("v2");
  });

  it("reads an unknown value, or blocked storage, as V2", async () => {
    expect((await load("v9")).getUiVersion()).toBe("v2");
    vi.stubGlobal("localStorage", {
      getItem: () => {
        throw new Error("blocked");
      },
    });
    vi.resetModules();
    expect((await import("./ui-version")).getUiVersion()).toBe("v2");
  });
});

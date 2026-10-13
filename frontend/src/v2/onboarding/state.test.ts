import { afterEach, describe, expect, it, vi } from "vitest";

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
  removeItem: () => {
    throw new Error("blocked");
  },
};

/** A fresh copy of the store module (its in-memory copy is module state). */
async function loadStore(storage: unknown) {
  vi.resetModules();
  vi.stubGlobal("localStorage", storage);
  return import("./state");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("launch checklist store", () => {
  it("keys the hidden flag per workspace", async () => {
    const store = await loadStore(memoryStorage());
    expect(store.launchHiddenKey("ws-1")).toBe("launchpad_v2_launch_hidden:ws-1");
  });

  it("is shown until hidden, and hiding one workspace leaves the others", async () => {
    const storage = memoryStorage();
    const store = await loadStore(storage);
    expect(store.isLaunchHidden("ws-1")).toBe(false);
    store.setLaunchHidden("ws-1", true);
    expect(store.isLaunchHidden("ws-1")).toBe(true);
    expect(store.isLaunchHidden("ws-2")).toBe(false);
    expect(storage.data.get("launchpad_v2_launch_hidden:ws-1")).toBe("1");
    store.setLaunchHidden("ws-1", false);
    expect(store.isLaunchHidden("ws-1")).toBe(false);
    expect(storage.data.has("launchpad_v2_launch_hidden:ws-1")).toBe(false);
  });

  it("reads a stored choice", async () => {
    const store = await loadStore(memoryStorage({ "launchpad_v2_launch_hidden:ws-1": "1" }));
    expect(store.isLaunchHidden("ws-1")).toBe(true);
  });

  it("a request un-hides it and bumps the counter", async () => {
    const storage = memoryStorage({ "launchpad_v2_launch_hidden:ws-1": "1" });
    const store = await loadStore(storage);
    expect(store.getLaunchRequest()).toBe(0);
    expect(store.takeLaunchScroll()).toBe(false);
    store.requestLaunchChecklist("ws-1");
    expect(store.isLaunchHidden("ws-1")).toBe(false);
    expect(store.getLaunchRequest()).toBe(1);
    // the scroll request is taken once
    expect(store.takeLaunchScroll()).toBe(true);
    expect(store.takeLaunchScroll()).toBe(false);
    // asking again while it is already shown still asks it to scroll into view
    store.requestLaunchChecklist("ws-1");
    expect(store.getLaunchRequest()).toBe(2);
    expect(store.takeLaunchScroll()).toBe(true);
  });

  it("works in memory when storage throws", async () => {
    const store = await loadStore(throwingStorage);
    expect(store.isLaunchHidden("ws-1")).toBe(false);
    expect(() => store.setLaunchHidden("ws-1", true)).not.toThrow();
    expect(store.isLaunchHidden("ws-1")).toBe(true);
    expect(() => store.requestLaunchChecklist("ws-1")).not.toThrow();
    expect(store.isLaunchHidden("ws-1")).toBe(false);
  });

  it("works in memory when there is no storage at all", async () => {
    const store = await loadStore(undefined);
    expect(store.isLaunchHidden("ws-1")).toBe(false);
    store.setLaunchHidden("ws-1", true);
    expect(store.isLaunchHidden("ws-1")).toBe(true);
  });
});

describe("tour store", () => {
  it("is not done until finished or skipped, then reads back from storage", async () => {
    const storage = memoryStorage();
    const store = await loadStore(storage);
    expect(store.TOUR_DONE_KEY).toBe("launchpad_v2_tour_done");
    expect(store.isTourDone()).toBe(false);
    store.setTourDone();
    expect(store.isTourDone()).toBe(true);
    expect(storage.data.get("launchpad_v2_tour_done")).toBe("1");
    store.setTourDone(false);
    expect(store.isTourDone()).toBe(false);
    expect(storage.data.has("launchpad_v2_tour_done")).toBe(false);
  });

  it("honours a flag a recording or script set before the page loaded", async () => {
    const store = await loadStore(memoryStorage({ launchpad_v2_tour_done: "1" }));
    expect(store.isTourDone()).toBe(true);
  });

  it("ignores any other stored value", async () => {
    const store = await loadStore(memoryStorage({ launchpad_v2_tour_done: "true" }));
    expect(store.isTourDone()).toBe(false);
  });

  it("counts replay requests without touching the done flag", async () => {
    const storage = memoryStorage({ launchpad_v2_tour_done: "1" });
    const store = await loadStore(storage);
    expect(store.getTourRequest()).toBe(0);
    expect(store.isTourRequested()).toBe(false);
    store.requestTour();
    store.requestTour();
    expect(store.getTourRequest()).toBe(2);
    expect(store.isTourDone()).toBe(true);
    // pending until taken, and taken once
    expect(store.isTourRequested()).toBe(true);
    expect(store.takeTourRequest()).toBe(true);
    expect(store.isTourRequested()).toBe(false);
    expect(store.takeTourRequest()).toBe(false);
  });

  it("works in memory when storage throws", async () => {
    const store = await loadStore(throwingStorage);
    expect(store.isTourDone()).toBe(false);
    expect(() => store.setTourDone()).not.toThrow();
    expect(store.isTourDone()).toBe(true);
    expect(() => store.requestTour()).not.toThrow();
  });

  it("works in memory when there is no storage at all", async () => {
    const store = await loadStore(undefined);
    expect(store.isTourDone()).toBe(false);
    store.setTourDone();
    expect(store.isTourDone()).toBe(true);
  });
});

import { afterEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_LAYOUT, FILES, STORED_MAX, WORKBENCH } from "./layout";
import { LAYOUT_KEY, loadLayoutPrefs, parseLayoutPrefs, saveLayoutPrefs } from "./layoutPrefs";

function fakeStorage(): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (k) => data.get(k) ?? null,
    key: (i) => [...data.keys()][i] ?? null,
    removeItem: (k) => void data.delete(k),
    setItem: (k, v) => void data.set(k, String(v)),
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("layout prefs", () => {
  it("round-trips one layout per browser", () => {
    const storage = fakeStorage();
    vi.stubGlobal("window", { localStorage: storage });
    expect(loadLayoutPrefs()).toEqual(DEFAULT_LAYOUT);
    const layout = { filesCollapsed: true, filesWidth: 300, workbenchCollapsed: false, workbenchWidth: 640 };
    saveLayoutPrefs(layout);
    expect(loadLayoutPrefs()).toEqual(layout);
    expect(JSON.parse(storage.getItem(LAYOUT_KEY) ?? "null")).toEqual(layout);
  });

  it("returns a fresh default object (callers may not mutate the shared one)", () => {
    const a = loadLayoutPrefs();
    expect(a).toEqual(DEFAULT_LAYOUT);
    expect(a).not.toBe(DEFAULT_LAYOUT);
  });

  it("survives missing or throwing storage and bad JSON", () => {
    expect(loadLayoutPrefs()).toEqual(DEFAULT_LAYOUT);
    expect(() => saveLayoutPrefs(DEFAULT_LAYOUT)).not.toThrow();
    vi.stubGlobal("window", {
      get localStorage(): Storage {
        throw new Error("denied");
      },
    });
    expect(loadLayoutPrefs()).toEqual(DEFAULT_LAYOUT);
    expect(() => saveLayoutPrefs(DEFAULT_LAYOUT)).not.toThrow();
    const full = fakeStorage();
    full.setItem = () => {
      throw new Error("QuotaExceededError");
    };
    vi.stubGlobal("window", { localStorage: full });
    expect(() => saveLayoutPrefs(DEFAULT_LAYOUT)).not.toThrow();
    const storage = fakeStorage();
    storage.setItem(LAYOUT_KEY, "{nope");
    vi.stubGlobal("window", { localStorage: storage });
    expect(loadLayoutPrefs()).toEqual(DEFAULT_LAYOUT);
  });

  it("validates every field on its own", () => {
    expect(parseLayoutPrefs(null)).toEqual(DEFAULT_LAYOUT);
    expect(parseLayoutPrefs([true])).toEqual(DEFAULT_LAYOUT);
    expect(parseLayoutPrefs("x")).toEqual(DEFAULT_LAYOUT);
    expect(
      parseLayoutPrefs({ filesCollapsed: "yes", filesWidth: "300", workbenchCollapsed: 1, workbenchWidth: Number.NaN }),
    ).toEqual(DEFAULT_LAYOUT);
    expect(parseLayoutPrefs({ filesCollapsed: true, workbenchWidth: 700 })).toEqual({
      ...DEFAULT_LAYOUT,
      filesCollapsed: true,
      workbenchWidth: 700,
    });
  });

  it("keeps widths only inside their sane range, rounded", () => {
    expect(parseLayoutPrefs({ filesWidth: FILES.min - 1 }).filesWidth).toBeNull();
    expect(parseLayoutPrefs({ filesWidth: FILES.max + 1 }).filesWidth).toBeNull();
    expect(parseLayoutPrefs({ filesWidth: 250.6 }).filesWidth).toBe(251);
    expect(parseLayoutPrefs({ workbenchWidth: WORKBENCH.min - 1 }).workbenchWidth).toBeNull();
    expect(parseLayoutPrefs({ workbenchWidth: STORED_MAX + 1 }).workbenchWidth).toBeNull();
    expect(parseLayoutPrefs({ workbenchWidth: Number.POSITIVE_INFINITY }).workbenchWidth).toBeNull();
    expect(parseLayoutPrefs({ workbenchWidth: 1200 }).workbenchWidth).toBe(1200);
  });
});

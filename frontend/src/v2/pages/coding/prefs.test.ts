import { afterEach, describe, expect, it, vi } from "vitest";

import { agentNameFrom, loadPrefs, pickOffered, savePrefs } from "./prefs";

const AGENT_NAME_RE = /^[a-z][a-z0-9-]{2,47}$/;

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

describe("composer prefs", () => {
  it("round-trips per project", () => {
    vi.stubGlobal("window", { localStorage: fakeStorage() });
    savePrefs("p1", { model: "m1", effort: "high" });
    expect(loadPrefs("p1")).toEqual({ model: "m1", effort: "high" });
    expect(loadPrefs("p2")).toEqual({});
  });

  it("survives missing or throwing storage and bad JSON", () => {
    expect(loadPrefs("p1")).toEqual({});
    expect(() => savePrefs("p1", { model: "m", effort: "low" })).not.toThrow();
    vi.stubGlobal("window", {
      get localStorage(): Storage {
        throw new Error("denied");
      },
    });
    expect(loadPrefs("p1")).toEqual({});
    expect(() => savePrefs("p1", { model: "m", effort: "low" })).not.toThrow();
    const storage = fakeStorage();
    storage.setItem("launchpad_coding_prefs:p1", "{nope");
    vi.stubGlobal("window", { localStorage: storage });
    expect(loadPrefs("p1")).toEqual({});
  });

  it("keeps a stored choice only while it is offered", () => {
    expect(pickOffered("b", "a", ["a", "b"])).toBe("b");
    expect(pickOffered("gone", "a", ["a", "b"])).toBe("a");
    expect(pickOffered(undefined, "z", ["a", "b"])).toBe("a");
    expect(pickOffered(null, "z", [])).toBe("z");
  });
});

describe("agentNameFrom", () => {
  it("slugifies a project name into a valid Agent name", () => {
    for (const [name, want] of [
      ["My Coding Project", "my-coding-project"],
      ["  hello__world!! ", "hello-world"],
      ["42 answers", "answers"],
      ["ab", "coding-ab"],
      ["项目", "coding-agent"],
      ["x".repeat(80), "x".repeat(48)],
    ] as const) {
      const got = agentNameFrom(name);
      expect(got).toBe(want);
      expect(AGENT_NAME_RE.test(got)).toBe(true);
    }
  });
});

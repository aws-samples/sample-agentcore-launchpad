import { describe, expect, it } from "vitest";

import {
  DISMISS_STORAGE_KEY,
  DISMISS_TTL_MS,
  type DismissStorage,
  dismissKey,
  loadScope,
  pruneStore,
  readStore,
  saveScope,
  visibleAttention,
  withDismissal,
  withoutDismissal,
} from "./attentionDismiss";
import type { Signal } from "./signals";

const NOW = Date.parse("2026-10-16T08:00:00Z");
const DAY = 24 * 60 * 60 * 1000;
const iso = (ms: number) => new Date(ms).toISOString();

function memory(initial?: unknown): DismissStorage & { raw: () => string | null } {
  const map = new Map<string, string>();
  if (initial !== undefined) map.set(DISMISS_STORAGE_KEY, typeof initial === "string" ? initial : JSON.stringify(initial));
  return {
    getItem: (k) => map.get(k) ?? null,
    setItem: (k, v) => void map.set(k, v),
    raw: () => map.get(DISMISS_STORAGE_KEY) ?? null,
  };
}

const throwing: DismissStorage = {
  getItem: () => {
    throw new Error("SecurityError");
  },
  setItem: () => {
    throw new Error("QuotaExceededError");
  },
};

const row = (key: string, signal: Signal = "act") => ({ key, signal });

describe("dismiss identity", () => {
  it("keys a row by its key and its signal", () => {
    expect(dismissKey(row("cfg:failed", "act"))).toBe("cfg:failed|act");
    expect(dismissKey(row("cfg:failed", "wait"))).not.toBe(dismissKey(row("cfg:failed", "act")));
  });

  it("shows a dismissed row again once its signal changes", () => {
    const dismissed = new Set([dismissKey(row("job:1", "wait"))]);
    expect(visibleAttention([row("job:1", "wait")], dismissed).shown).toEqual([]);
    expect(visibleAttention([row("job:1", "act")], dismissed).shown).toEqual([row("job:1", "act")]);
  });
});

describe("visible attention", () => {
  const items = ["a", "b", "c", "d", "e", "f", "g"].map((k) => row(k));

  it("keeps every row and counts them all with nothing dismissed", () => {
    expect(visibleAttention(items, new Set())).toEqual({ shown: items, dismissed: [], remaining: 7 });
    expect(visibleAttention([], new Set(), 5)).toEqual({ shown: [], dismissed: [], remaining: 0 });
  });

  it("filters before it caps, so a dismissed shown row promotes the next", () => {
    expect(visibleAttention(items, new Set(), 5).shown.map((i) => i.key)).toEqual(["a", "b", "c", "d", "e"]);
    const view = visibleAttention(items, new Set(["b|act"]), 5);
    expect(view.shown.map((i) => i.key)).toEqual(["a", "c", "d", "e", "f"]);
    expect(view.dismissed.map((i) => i.key)).toEqual(["b"]);
    expect(view.remaining).toBe(6);
  });

  it("counts the rows not dismissed, past the cap; a stale stored key changes nothing", () => {
    const view = visibleAttention(items, new Set(["g|act", "gone|act", "a|wait"]), 3);
    expect(view.shown.map((i) => i.key)).toEqual(["a", "b", "c"]);
    expect(view.dismissed.map((i) => i.key)).toEqual(["g"]);
    expect(view.remaining).toBe(6);
  });

  it("shows nothing when everything is dismissed", () => {
    const all = new Set(items.map(dismissKey));
    expect(visibleAttention(items, all, 5)).toEqual({ shown: [], dismissed: items, remaining: 0 });
  });
});

describe("dismiss storage", () => {
  it("round-trips one scope and leaves the others alone", () => {
    const storage = memory({ registry: { "r1:pending|wait": iso(NOW - DAY) } });
    const entries = withDismissal(loadScope("eval.tasks", storage, NOW), "run:x|act", NOW);
    saveScope("eval.tasks", entries, storage, NOW);
    expect(JSON.parse(storage.raw() ?? "")).toEqual({
      registry: { "r1:pending|wait": iso(NOW - DAY) },
      "eval.tasks": { "run:x|act": iso(NOW) },
    });
    expect(loadScope("eval.tasks", storage, NOW)).toEqual({ "run:x|act": iso(NOW) });
  });

  it("restores by removing the key; an emptied scope is dropped", () => {
    const storage = memory({ canary: { "c1|wait": iso(NOW) } });
    saveScope("canary", withoutDismissal(loadScope("canary", storage, NOW), "c1|wait"), storage, NOW);
    expect(JSON.parse(storage.raw() ?? "")).toEqual({});
    expect(loadScope("canary", storage, NOW)).toEqual({});
  });

  it("prunes dismissals older than 30 days from every scope on write", () => {
    const storage = memory({
      online: { edge: iso(NOW - DISMISS_TTL_MS), old: iso(NOW - DISMISS_TTL_MS - 1) },
      skilllab: { stale: iso(NOW - 40 * DAY) },
    });
    saveScope("eval.agents", { "failed:r|act": iso(NOW) }, storage, NOW);
    expect(JSON.parse(storage.raw() ?? "")).toEqual({
      online: { edge: iso(NOW - DISMISS_TTL_MS) },
      "eval.agents": { "failed:r|act": iso(NOW) },
    });
  });

  it("ignores an expired dismissal on read as well", () => {
    const storage = memory({ online: { old: iso(NOW - 31 * DAY), fresh: iso(NOW - DAY) } });
    expect(loadScope("online", storage, NOW)).toEqual({ fresh: iso(NOW - DAY) });
  });

  it("reads garbage, a wrong shape or bad dates as nothing dismissed", () => {
    expect(readStore(memory("{not json"), NOW)).toEqual({});
    expect(readStore(memory([1, 2]), NOW)).toEqual({});
    expect(readStore(memory({ a: "x", b: [1], c: { k: 5, j: "nope", ok: iso(NOW) } }), NOW)).toEqual({ c: { ok: iso(NOW) } });
    expect(readStore(memory(), NOW)).toEqual({});
    expect(pruneStore(null, NOW)).toEqual({});
  });

  it("treats throwing or missing storage as nothing dismissed, and never throws", () => {
    expect(readStore(throwing, NOW)).toEqual({});
    expect(loadScope("governance", throwing, NOW)).toEqual({});
    expect(() => saveScope("governance", { "gw|act": iso(NOW) }, throwing, NOW)).not.toThrow();
    expect(loadScope("governance", null, NOW)).toEqual({});
    expect(() => saveScope("governance", { "gw|act": iso(NOW) }, null, NOW)).not.toThrow();
    // the vitest node environment has no localStorage: the default is "unavailable"
    expect(loadScope("governance")).toEqual({});
    expect(() => saveScope("governance", { "gw|act": iso(NOW) })).not.toThrow();
  });

  it("keeps a write going when only the write throws", () => {
    const readOnly: DismissStorage = {
      getItem: () => JSON.stringify({ workspaces: { "w|act": iso(NOW) } }),
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
    };
    expect(() => saveScope("workspaces", {}, readOnly, NOW)).not.toThrow();
    expect(loadScope("workspaces", readOnly, NOW)).toEqual({ "w|act": iso(NOW) });
  });
});

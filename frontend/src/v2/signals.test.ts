import { describe, expect, it } from "vitest";

import { rankBySignal, SIGNAL_TONE, type Signal, worstSignal } from "./signals";

describe("signals", () => {
  it("gives every signal its own tag tone", () => {
    const tones = Object.values(SIGNAL_TONE);
    expect(Object.keys(SIGNAL_TONE).sort()).toEqual(["act", "info", "off", "ok", "wait"]);
    expect(new Set(tones).size).toBe(tones.length);
  });

  it("ranks act > wait > info > ok > off", () => {
    const order: Signal[] = ["off", "ok", "info", "wait", "act"];
    order.forEach((s, i) => expect(worstSignal(order.slice(0, i + 1))).toBe(s));
    expect(worstSignal(["ok", "act", "wait"])).toBe("act");
  });

  it("has no worst signal for an empty list", () => {
    expect(worstSignal([])).toBeUndefined();
  });

  it("ranks items act > wait > info > ok > off without mutating the input", () => {
    const items: { signal: Signal }[] = ["off", "ok", "info", "wait", "act"].map((signal) => ({ signal: signal as Signal }));
    const ranked = rankBySignal(items);
    expect(ranked.map((i) => i.signal)).toEqual(["act", "wait", "info", "ok", "off"]);
    expect(items.map((i) => i.signal)).toEqual(["off", "ok", "info", "wait", "act"]);
  });

  it("keeps the given order within one signal", () => {
    const items: { signal: Signal; id: number }[] = [
      { signal: "wait", id: 1 },
      { signal: "act", id: 2 },
      { signal: "wait", id: 3 },
      { signal: "act", id: 4 },
      { signal: "wait", id: 5 },
    ];
    expect(rankBySignal(items).map((i) => i.id)).toEqual([2, 4, 1, 3, 5]);
    expect(rankBySignal([])).toEqual([]);
  });
});
